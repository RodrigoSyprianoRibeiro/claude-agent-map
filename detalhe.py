"""Detalhe de uma sessao: subagentes, advisor, custo por modelo e linha do tempo de ferramentas.

O custo e estimado pelos PRECOS por modelo (USD por milhao de tokens) definidos aqui: atualize a
tabela quando os precos da API mudarem.

Leitura incremental por arquivo, com cache entre chamadas. Transcrito principal pode passar de
100 MB: na primeira leitura so o final e lido (por isso o custo por modelo da sessao principal e
parcial, e a resposta avisa em `parcial`). Subagentes sao pequenos e lidos inteiros. A deduplicacao
e por message.id: o streaming grava a mesma mensagem varias vezes e vale a ULTIMA linha.
So biblioteca padrao.
"""

import collections
import glob
import json
import os
import re
import threading
import time
from datetime import datetime
from urllib.parse import urlparse

# USD por milhao de tokens: entrada, escrita cache 5m, escrita cache 1h, leitura cache, saida.
# Fonte: platform.claude.com/docs/en/about-claude/pricing (consultado em 08/10/2026).
PRECOS = {
    'claude-fable-5-1': (10, 12.5, 20, 0.25, 50),
    'claude-fable-5': (10, 12.5, 20, 1, 50),
    'claude-opus-5-5': (4, 5, 8, 0.20, 20),
    'claude-opus-5': (5, 6.25, 10, 0.50, 25),
    'claude-opus-4': (5, 6.25, 10, 0.50, 25),
    'claude-sonnet-5-5': (2, 2.5, 4, 0.10, 10),
    'claude-sonnet-5': (2, 2.5, 4, 0.20, 10),
    'claude-sonnet-4': (3, 3.75, 6, 0.30, 15),
    'claude-haiku-5-5': (0.10, 0.125, 0.20, 0.01, 0.50),
    'claude-haiku-4-5': (1, 1.25, 2, 0.10, 5),
}
# Haiku 5.5 cobra 5x quando o prompt passa de 100 mil tokens
PRECO_HAIKU55_LONGO = (0.50, 0.625, 1, 0.05, 2.50)
LIMITE_HAIKU55_LONGO = 100_000
MODELO_REFERENCIA = 'claude-opus-5-5'

def preco_do_modelo(modelo, tokens_prompt=0):
    """Tabela de preco pelo prefixo mais longo do id (ignora sufixo de data ou [1m])."""
    if not modelo:
        return None
    modelo = modelo.split('[')[0]
    if modelo.startswith('claude-haiku-5-5') and tokens_prompt > LIMITE_HAIKU55_LONGO:
        return PRECO_HAIKU55_LONGO
    melhor = None
    for prefixo in PRECOS:
        if modelo.startswith(prefixo) and (melhor is None or len(prefixo) > len(melhor)):
            melhor = prefixo
    return PRECOS[melhor] if melhor else None


def custo_usage(usage, modelo):
    """Custo em USD de um bloco de usage. None quando o modelo nao tem preco conhecido."""
    entrada = usage.get('input_tokens') or 0
    leitura = usage.get('cache_read_input_tokens') or 0
    escrita = usage.get('cache_creation_input_tokens') or 0
    detalhe = usage.get('cache_creation') or {}
    escrita_1h = detalhe.get('ephemeral_1h_input_tokens') or 0
    escrita_5m = detalhe.get('ephemeral_5m_input_tokens')
    if escrita_5m is None:
        escrita_5m = escrita - escrita_1h
    saida = usage.get('output_tokens') or 0
    preco = preco_do_modelo(modelo, entrada + leitura + escrita)
    if preco is None:
        return None
    return (entrada * preco[0] + escrita_5m * preco[1] + escrita_1h * preco[2]
            + leitura * preco[3] + saida * preco[4]) / 1e6


def partes_da_mensagem(modelo, usage):
    """Quebra o usage em (modelo, usage, e_advisor). Com advisor, o usage traz iterations por modelo."""
    iteracoes = usage.get('iterations') or []
    if not iteracoes:
        return [(modelo, usage, False)]
    partes = []
    for it in iteracoes:
        advisor = it.get('type') == 'advisor_message'
        partes.append((it.get('model') or modelo, it, advisor))
    return partes


def resumo_ferramenta(nome, entrada):
    """Uma linha curta do que a ferramenta fez, sem despejar conteudo de arquivo."""
    entrada = entrada if isinstance(entrada, dict) else {}
    if nome == 'Bash':
        texto = entrada.get('description') or entrada.get('command') or ''
    elif nome in ('Read', 'Edit', 'Write', 'NotebookEdit'):
        texto = os.path.basename(entrada.get('file_path') or '')
    elif nome in ('Grep', 'Glob'):
        texto = entrada.get('pattern') or ''
    elif nome == 'Agent':
        texto = '%s (%s)' % (entrada.get('description') or '', entrada.get('subagent_type') or 'general-purpose')
        if entrada.get('model'):
            texto += ' -> ' + entrada['model']
    elif nome == 'Skill':
        texto = entrada.get('skill') or ''
    elif nome in ('WebFetch', 'WebSearch'):
        texto = urlparse(entrada.get('url') or '').netloc or entrada.get('query') or ''
    else:
        texto = ''
    texto = ' '.join(str(texto).split())
    return texto[:90]


SEGUNDOS_RODANDO = 90
LEITURA_PRINCIPAL = 4_000_000   # bytes do final lidos na primeira vez
MAX_FERRAMENTAS = 40
MAX_CACHE = 80
ID_VALIDO = re.compile(r'^[0-9A-Za-z-]{8,64}$')


def epoch(carimbo):
    if not isinstance(carimbo, str):
        return None
    try:
        return datetime.fromisoformat(carimbo.replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


class Transcrito:
    """Estado de um .jsonl lido de forma incremental por offset de bytes."""

    def __init__(self, caminho, leitura_inicial=None):
        self.caminho = caminho
        self.leitura_inicial = leitura_inicial  # None = arquivo inteiro
        self.parcial = False
        self.offset = None
        self.resto = b''
        self.mtime = 0
        self.acesso = time.time()
        self.mensagens = {}   # id -> (modelo, usage); a ultima linha do streaming vence
        self.ferramentas = {}  # id do tool_use -> evento
        self.advisor = {}     # id da mensagem -> evento de consulta ao advisor
        self.modelo = None
        self.parada = None    # stop_reason da ultima linha do assistente
        self.primeiro_ts = None
        self.ultimo_ts = None

    def atualizar(self):
        self.acesso = time.time()
        try:
            estado = os.stat(self.caminho)
        except FileNotFoundError:
            return
        self.mtime = estado.st_mtime
        if self.offset is not None and estado.st_size < self.offset:  # reescrito
            self.__init__(self.caminho, self.leitura_inicial)
        descartar_primeira = False
        if self.offset is None:
            inicio = 0
            if self.leitura_inicial and estado.st_size > self.leitura_inicial:
                inicio = estado.st_size - self.leitura_inicial
                descartar_primeira = True
                self.parcial = True
            self.offset = inicio
        if estado.st_size == self.offset:
            return
        with open(self.caminho, 'rb') as arq:
            arq.seek(self.offset)
            bloco = arq.read(estado.st_size - self.offset)
        self.offset += len(bloco)
        linhas = (self.resto + bloco).split(b'\n')
        self.resto = linhas.pop()  # linha incompleta fica para a proxima leitura
        if descartar_primeira and linhas:
            linhas.pop(0)  # comecou no meio de uma linha
        for linha in linhas:
            if linha.strip():
                self._processar(linha)

    def _processar(self, linha):
        try:
            j = json.loads(linha)
        except ValueError:
            return
        if not isinstance(j, dict):
            return
        ts = epoch(j.get('timestamp'))
        if ts is not None:
            if self.primeiro_ts is None or ts < self.primeiro_ts:
                self.primeiro_ts = ts
            if self.ultimo_ts is None or ts > self.ultimo_ts:
                self.ultimo_ts = ts
        if j.get('type') != 'assistant':
            return
        msg = j.get('message')
        if not isinstance(msg, dict):
            return
        modelo = msg.get('model')
        if modelo and not modelo.startswith('<'):
            self.modelo = modelo
        self.parada = msg.get('stop_reason')
        if msg.get('id') and isinstance(msg.get('usage'), dict):
            self.mensagens[msg['id']] = (modelo, msg['usage'])
            self.advisor.pop(msg['id'], None)  # a ultima linha decide se houve consulta
            for parte_modelo, parte, advisor in partes_da_mensagem(modelo, msg['usage']):
                if advisor:
                    lidos = sum((parte.get(k) or 0) for k in (
                        'input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'))
                    self.advisor[msg['id']] = {
                        'ts': ts, 'modelo': (parte_modelo or '?').split('[')[0], 'tokensLidos': lidos,
                        'tokensSaida': parte.get('output_tokens') or 0, 'custo': custo_usage(parte, parte_modelo) or 0.0,
                    }
        conteudo = msg.get('content') if isinstance(msg.get('content'), list) else []
        for bloco in conteudo:
            if isinstance(bloco, dict) and bloco.get('type') == 'tool_use' and bloco.get('id'):
                nome = bloco.get('name') or ''
                self.ferramentas[bloco['id']] = {
                    'ts': ts,
                    # mcp__servidor__acao vira servidor:acao
                    'ferramenta': ':'.join(nome.split('__')[1:]) if nome.startswith('mcp__') else nome,
                    'resumo': resumo_ferramenta(nome, bloco.get('input')),
                }

    def custos(self):
        """{modelo: [custo, tokens, chamadas]}; o advisor aparece como modelo proprio dentro do mesmo dict."""
        por_modelo = {}
        for modelo, usage in self.mensagens.values():
            for parte_modelo, parte, _ in partes_da_mensagem(modelo, usage):
                custo = custo_usage(parte, parte_modelo)
                if custo is None:
                    continue
                tokens = sum((parte.get(k) or 0) for k in (
                    'input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens', 'output_tokens'))
                acum = por_modelo.setdefault((parte_modelo or '?').split('[')[0], [0.0, 0, 0])
                acum[0] += custo
                acum[1] += tokens
                acum[2] += 1
        return por_modelo


class Detalhes:
    def __init__(self, base):
        self.base = base  # ~/.claude
        self.cache = {}
        self.trava = threading.Lock()

    def _transcrito(self, caminho, leitura_inicial=None):
        t = self.cache.get(caminho)
        if t is None:
            if len(self.cache) >= MAX_CACHE:  # descarta o menos recente
                self.cache.pop(min(self.cache, key=lambda c: self.cache[c].acesso))
            t = self.cache[caminho] = Transcrito(caminho, leitura_inicial)
        t.atualizar()
        return t

    def localizar(self, sessao_id):
        if not ID_VALIDO.match(sessao_id or ''):
            return None
        achados = glob.glob(os.path.join(self.base, 'projects', '*', sessao_id + '.jsonl'))
        return achados[0] if achados else None

    def sessao(self, sessao_id, agora=None):
        """Dicionario do detalhe, ou None se a sessao nao existe (ou o id e invalido)."""
        agora = agora or time.time()
        caminho = self.localizar(sessao_id)
        if caminho is None:
            return None
        with self.trava:
            principal = self._transcrito(caminho, LEITURA_PRINCIPAL)
            subagentes, custos_totais = [], {}
            advisor = list(principal.advisor.values())
            for modelo, (c, t, n) in principal.custos().items():
                acum = custos_totais.setdefault(modelo, [0.0, 0, 0])
                acum[0] += c; acum[1] += t; acum[2] += n
            ferramentas = [dict(f, agente='principal') for f in principal.ferramentas.values()]
            pasta = caminho[:-6] + os.sep + 'subagents'
            for arq in sorted(glob.glob(os.path.join(pasta, 'agent-*.jsonl'))):
                sub = self._transcrito(arq)
                meta = {}
                try:
                    with open(arq[:-6] + '.meta.json') as f:
                        meta = json.load(f)
                except (OSError, ValueError):
                    pass
                if sub.parada == 'end_turn':
                    status = 'concluido'
                elif agora - sub.mtime < SEGUNDOS_RODANDO:
                    status = 'rodando'
                else:
                    status = 'parado'
                por_modelo = sub.custos()
                for modelo, (c, t, n) in por_modelo.items():
                    acum = custos_totais.setdefault(modelo, [0.0, 0, 0])
                    acum[0] += c; acum[1] += t; acum[2] += n
                ferrs = sorted(sub.ferramentas.values(), key=lambda e: e['ts'] or 0)
                advisor += list(sub.advisor.values())
                inicio, fim = sub.primeiro_ts, sub.ultimo_ts
                subagentes.append({
                    'id': os.path.basename(arq)[:-6],
                    'tipo': meta.get('agentType') or 'subagente',
                    'descricao': meta.get('description') or '',
                    'modelo': (sub.modelo or '').split('[')[0] or None,
                    'status': status,
                    'inicio': inicio, 'fim': fim,
                    'duracao': (fim - inicio) if inicio is not None and fim is not None else None,
                    'ferramentas': len(ferrs),
                    'ultima': ferrs[-1] if ferrs else None,
                    'custo': sum(v[0] for v in por_modelo.values()),
                    'atualizadoEm': sub.mtime,
                })
            subagentes.sort(key=lambda s: s['inicio'] or 0)
            advisor.sort(key=lambda e: e['ts'] or 0)
            ferramentas.sort(key=lambda e: e['ts'] or 0)
            return {
                'id': sessao_id, 'geradoEm': agora, 'parcial': principal.parcial,
                'principal': {'modelo': (principal.modelo or '').split('[')[0] or None},
                'subagentes': subagentes,
                'advisor': {'consultas': advisor, 'custo': sum(e['custo'] for e in advisor)},
                'custoPorModelo': sorted(
                    ({'modelo': m, 'custo': v[0], 'tokens': v[1], 'chamadas': v[2]} for m, v in custos_totais.items()),
                    key=lambda x: -x['custo']),
                'ferramentas': ferramentas[-MAX_FERRAMENTAS:],
            }

