"""Conversa de uma sessao lida do transcrito (.jsonl), no formato que a vista de chat desenha.

O transcrito pode passar de 100 MB: a primeira leitura pega so o final (JANELA bytes) e as
seguintes so o que foi acrescentado, pelo offset de byte que o cliente devolve (`desde`).
"Carregar anteriores" le a janela que termina em `ate`. Linha sem '\\n' no fim ainda esta sendo
escrita: fica para a proxima leitura.

Cada bloco do assistente vem numa linha propria (mesma message.id em varias linhas), por isso a
chave de cada item e o uuid da LINHA, nunca a message.id. Textos longos sao cortados aqui: a
pagina so mostra, nao precisa do conteudo inteiro de arquivo nem de saida de comando.
So biblioteca padrao.
"""

import json
import os
import re

import detalhe

JANELA = 3_000_000          # bytes lidos do final na primeira vez (e por "carregar anteriores")
MAX_INCREMENTO = 8_000_000  # bytes por leitura incremental; `mais` avisa que ha mais
MAX_TEXTO = 200_000         # resposta do assistente / prompt
MAX_SAIDA = 6_000           # resultado de ferramenta
MAX_CAMPO = 4_000           # cada campo da entrada de ferramenta
TIPOS_IGNORADOS = {'mode', 'permission-mode', 'ai-title', 'last-prompt', 'file-history-snapshot', 'agent-name',
                   'atis-latch', 'attachment', 'summary', 'custom-title', 'queue-operation'}
ANSI = re.compile(r'\x1b\[[0-9;?]*[A-Za-z]')
TAG = re.compile(r'<([a-z][a-z0-9-]*)>(.*?)</\1>', re.S)


def cortar(texto, limite):
    """(texto, quantos caracteres ficaram de fora)."""
    texto = texto if isinstance(texto, str) else str(texto)
    return (texto, 0) if len(texto) <= limite else (texto[:limite], len(texto) - limite)


def entrada_resumida(entrada):
    """Entrada da ferramenta com cada campo cortado; nao-texto vira JSON."""
    if not isinstance(entrada, dict):
        return {}
    saida = {}
    for chave, valor in list(entrada.items())[:20]:
        if not isinstance(valor, str):
            valor = json.dumps(valor, ensure_ascii=False, indent=1)
        saida[chave] = cortar(valor, MAX_CAMPO)[0]
    return saida


def texto_de_blocos(conteudo):
    """(texto, imagens) de um content de tool_result ou de mensagem: string ou lista de blocos."""
    if isinstance(conteudo, str):
        return conteudo, 0
    textos, imagens = [], 0
    for b in conteudo if isinstance(conteudo, list) else []:
        if not isinstance(b, dict):
            continue
        if b.get('type') == 'text':
            textos.append(b.get('text') or '')
        elif b.get('type') == 'image':
            imagens += 1
    return '\n'.join(textos), imagens


def tags(texto):
    return {m.group(1): m.group(2).strip() for m in TAG.finditer(texto)}


def aviso_do_envelope(texto):
    """Envelopes do harness (comando /x, saida de comando local, ! bash) viram aviso; o resto e ignorado (None)."""
    t = tags(texto)
    if 'command-name' in t:
        return ('comando', (t['command-name'] + ' ' + t.get('command-args', '')).strip())
    if 'bash-input' in t:
        return ('comando', '! ' + t['bash-input'])
    for chave in ('local-command-stdout', 'bash-stdout', 'bash-stderr'):
        if t.get(chave):
            return ('saida', cortar(ANSI.sub('', t[chave]), MAX_SAIDA)[0])
    return None


def itens_da_linha(j):
    """Itens da vista para uma linha do transcrito (lista possivelmente vazia)."""
    if not isinstance(j, dict) or j.get('isSidechain') or j.get('type') in TIPOS_IGNORADOS:
        return []
    base = {'u': j.get('uuid'), 'ts': detalhe.epoch(j.get('timestamp'))}
    tipo = j.get('type')
    if tipo == 'system':
        if j.get('subtype') == 'compact_boundary':
            return [dict(base, t='aviso', tipo='compactado', texto='Conversa compactada')]
        return []
    msg = j.get('message')
    if not isinstance(msg, dict):
        return []
    conteudo = msg.get('content')

    if tipo == 'assistant':
        if j.get('isApiErrorMessage'):
            return [dict(base, t='aviso', tipo='erro', texto=cortar(texto_de_blocos(conteudo)[0], MAX_SAIDA)[0])]
        itens = []
        for b in conteudo if isinstance(conteudo, list) else []:
            if not isinstance(b, dict):
                continue
            if b.get('type') == 'text' and (b.get('text') or '').strip():
                itens.append(dict(base, t='texto', texto=cortar(b['text'], MAX_TEXTO)[0], modelo=msg.get('model')))
            elif b.get('type') == 'thinking':
                # o texto costuma vir vazio (so a assinatura); nunca mandar a assinatura
                itens.append(dict(base, t='pensou', msg=msg.get('id'), texto=cortar(b.get('thinking') or '', MAX_TEXTO)[0]))
            elif b.get('type') in ('tool_use', 'server_tool_use'):
                nome = b.get('name') or '?'
                itens.append(dict(base, t='ferramenta', id=b.get('id'), nome=nome,
                                  resumo=detalhe.resumo_ferramenta(nome, b.get('input')), entrada=entrada_resumida(b.get('input'))))
        return itens

    if tipo != 'user':
        return []
    if j.get('isCompactSummary'):
        return [dict(base, t='aviso', tipo='compactado', texto='Conversa compactada (resumo do que veio antes)')]
    if isinstance(conteudo, list) and any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in conteudo):
        itens = []
        for b in conteudo:
            if isinstance(b, dict) and b.get('type') == 'tool_result':
                texto, imagens = texto_de_blocos(b.get('content'))
                texto, fora = cortar(ANSI.sub('', texto), MAX_SAIDA)
                itens.append(dict(base, t='resultado', id=b.get('tool_use_id'), texto=texto, cortado=fora,
                                  imagens=imagens, erro=bool(b.get('is_error'))))
        return itens
    if j.get('isMeta'):
        return []
    texto, imagens = texto_de_blocos(conteudo)
    if not detalhe_humano(j):
        # task-notification traz <summary>; outras origens (mensagem de agente) ficam com o texto sem tags
        achado = re.search(r'<summary>(.*?)</summary>', texto, re.S)
        resumo = ' '.join((achado.group(1) if achado else re.sub(r'<[^>]+>', ' ', texto)).split())
        return [dict(base, t='aviso', tipo='notificacao', texto=cortar(resumo, 400)[0])] if resumo else []
    limpo = texto.strip()
    if limpo.startswith('[Request interrupted by user'):
        return [dict(base, t='aviso', tipo='interrompido', texto='Interrompido')]
    if limpo.startswith('<'):
        env = aviso_do_envelope(limpo)
        if env and env[1]:
            return [dict(base, t='aviso', tipo=env[0], texto=env[1])]
        if not imagens:
            return []  # system-reminder, caveat e afins
        limpo = ''
    return [dict(base, t='usuario', texto=cortar(limpo, MAX_TEXTO)[0], imagens=imagens)] if limpo or imagens else []


def detalhe_humano(j):
    """Mesma regra do painel: origin.kind diferente de human (task-notification, agentes) nao e prompt."""
    origem = j.get('origin')
    return not isinstance(origem, dict) or origem.get('kind') in (None, 'human')


def _inicio_de_linha(f, pos):
    """Avanca `pos` ate o comeco da proxima linha completa (0 ja e comeco)."""
    if pos <= 0:
        return 0
    f.seek(pos - 1)
    if f.read(1) == b'\n':
        return pos
    f.readline()
    return f.tell()


def ler(caminho, desde=None, ate=None):
    """Itens a partir de `desde` (offset devolvido antes), a janela que termina em `ate`, ou o final do arquivo.

    Devolve {itens, inicio, cursor, tamanho, mais, reiniciar}. `inicio` > 0 = ha conversa antes (use `ate=inicio`).
    `reiniciar` = o arquivo encolheu (foi reescrito): o cliente descarta o que tem e pede de novo sem `desde`."""
    tamanho = os.path.getsize(caminho)
    if desde is not None and desde > tamanho:
        return {'itens': [], 'inicio': 0, 'cursor': 0, 'tamanho': tamanho, 'mais': False, 'reiniciar': True}
    with open(caminho, 'rb') as f:
        if ate is not None:
            ate = min(ate, tamanho)
            inicio, limite = _inicio_de_linha(f, max(0, ate - JANELA)), ate
        elif desde is None:
            inicio, limite = _inicio_de_linha(f, max(0, tamanho - JANELA)), tamanho
        else:
            inicio, limite = desde, min(tamanho, desde + MAX_INCREMENTO)
        f.seek(inicio)
        pos, itens = inicio, []
        while pos < limite:
            linha = f.readline()
            if not linha.endswith(b'\n'):
                break  # ainda sendo escrita
            pos += len(linha)
            try:
                j = json.loads(linha)
            except ValueError:
                continue
            itens.extend(itens_da_linha(j))
    if ate is not None:
        return {'itens': itens, 'inicio': inicio, 'cursor': None, 'tamanho': tamanho, 'mais': False, 'reiniciar': False}
    return {'itens': itens, 'inicio': inicio, 'cursor': pos, 'tamanho': tamanho, 'mais': pos < tamanho and pos >= limite,
            'reiniciar': False}
