"""Uso do plano (janelas de 5h e 7d) gravado pelo statusLine em ~/.claude/state/uso-plano.json.

O Claude Code manda `rate_limits` no JSON do statusLine (so assinantes Pro/Max e so depois da
primeira resposta da API da sessao); ~/.claude/hooks/status-uso.py grava esse bloco. Aqui so
se le o arquivo e se calcula o que a tela mostra. So biblioteca padrao.
"""

import json
import os
import time

ARQUIVO = os.path.expanduser('~/.claude/state/uso-plano.json')
JANELAS = ('five_hour', 'seven_day', 'spend_limit')
VELHO_SEGUNDOS = 30 * 60


def _num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def ler(caminho=ARQUIVO):
    try:
        with open(caminho) as arq:
            dados = json.load(arq)
    except (OSError, ValueError):
        return None
    return dados if isinstance(dados, dict) else None


def calcular(dados, agora=None):
    """Estado para a tela. Janela com resets_at no passado conta como zerada (0%, sem reset)."""
    agora = agora or time.time()
    if not dados:
        return {'disponivel': False}
    saida = {'disponivel': True, 'sessao': dados.get('sessao')}
    for nome in JANELAS:
        j = dados.get(nome)
        if not isinstance(j, dict) or _num(j.get('used_percentage')) is None:
            continue
        reset = _num(j.get('resets_at'))
        zerada = reset is not None and reset <= agora
        item = {k: v for k, v in j.items() if k in ('used_usd', 'limit_usd', 'period')}
        item['usado'] = 0.0 if zerada else float(j['used_percentage'])
        item['resetsEm'] = None if (zerada or reset is None) else int(reset - agora)
        item['zerada'] = zerada
        saida[nome] = item
    atualizado = _num(dados.get('atualizadoEm'))
    saida['atualizadoEm'] = atualizado
    saida['idade'] = None if atualizado is None else max(0, int(agora - atualizado))
    saida['velho'] = saida['idade'] is None or saida['idade'] > VELHO_SEGUNDOS
    saida['disponivel'] = any(n in saida for n in JANELAS)
    return saida
