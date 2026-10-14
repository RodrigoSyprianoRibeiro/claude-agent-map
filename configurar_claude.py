#!/usr/bin/env python3
"""Registra (ou remove) os hooks do Mapa de agentes no ~/.claude/settings.json do usuario.

- statusLine -> hooks/status-uso.py (grava o uso do plano). Se ja existe um statusLine, ele NAO e
  substituido: vira `--encadear '<original>'`, e a linha que a pessoa via continua a mesma.
- Notification -> hooks/notificacao-sessao.py (marca a sessao que precisa de voce), somado aos
  hooks que ja existirem.

Antes de gravar, copia o settings.json para settings.json.bak-mapa-agentes-<data>. Idempotente.
`remover` desfaz exatamente isso (e devolve o statusLine original).

Uso: python3 configurar_claude.py install|uninstall [--settings caminho]
So biblioteca padrao.
"""

import argparse
import json
import os
import shlex
import shutil
import sys
import tempfile
import time

AQUI = os.path.dirname(os.path.abspath(__file__))
SETTINGS = os.path.expanduser('~/.claude/settings.json')
MARCA_USO = 'status-uso.py'
MARCA_ALERTA = 'notificacao-sessao'


def comando_uso(pasta, original=None):
    cmd = 'python3 %s' % shlex.quote(os.path.join(pasta, 'hooks', 'status-uso.py'))
    return cmd + (' --encadear %s' % shlex.quote(original) if original else '')


def comando_alerta(pasta):
    return 'python3 %s' % shlex.quote(os.path.join(pasta, 'hooks', 'notificacao-sessao.py'))


def original_encadeado(comando):
    """Comando original guardado em --encadear, ou None."""
    try:
        partes = shlex.split(comando)
    except ValueError:
        return None
    if '--encadear' in partes and partes.index('--encadear') + 1 < len(partes):
        return partes[partes.index('--encadear') + 1]
    return None


def ler(caminho):
    try:
        with open(caminho) as arq:
            dados = json.load(arq)
    except FileNotFoundError:
        return {}
    if not isinstance(dados, dict):
        raise ValueError('%s is not a JSON object' % caminho)
    return dados


def gravar(caminho, dados):
    pasta = os.path.dirname(caminho)
    os.makedirs(pasta, exist_ok=True)
    if os.path.exists(caminho):  # backup nunca sobrescreve outro (duas gravacoes no mesmo segundo)
        base = '%s.bak-mapa-agentes-%s' % (caminho, time.strftime('%Y%m%d-%H%M%S'))
        destino, n = base, 1
        while os.path.exists(destino):
            n += 1
            destino = '%s-%d' % (base, n)
        shutil.copy2(caminho, destino)
    fd, tmp = tempfile.mkstemp(dir=pasta, suffix='.tmp')
    with os.fdopen(fd, 'w') as arq:
        json.dump(dados, arq, indent=2, ensure_ascii=False)
        arq.write('\n')
    os.replace(tmp, caminho)


def instalar(dados, pasta=AQUI):
    """Devolve (dados, mensagens). Nao grava nada."""
    msgs = []
    atual = dados.get('statusLine')
    cmd_atual = atual.get('command') if isinstance(atual, dict) else None
    if not cmd_atual:
        dados['statusLine'] = {'type': 'command', 'command': comando_uso(pasta), 'refreshInterval': 60}
        msgs.append('statusLine: plan usage hook added')
    elif MARCA_USO in cmd_atual:
        msgs.append('statusLine: plan usage hook already present')
    else:
        dados['statusLine'] = dict(atual, command=comando_uso(pasta, cmd_atual))
        msgs.append('statusLine: yours was kept (it runs right after the usage hook)')

    hooks = dados.setdefault('hooks', {})
    if not isinstance(hooks, dict):
        raise ValueError('"hooks" in settings.json is not an object')
    lista = hooks.setdefault('Notification', [])
    ja = any(MARCA_ALERTA in str(h.get('command', '')) for grupo in lista if isinstance(grupo, dict)
             for h in grupo.get('hooks', []) if isinstance(h, dict))
    if ja:
        msgs.append('Notification: "needs you" alert already present')
    else:
        lista.append({'hooks': [{'type': 'command', 'command': comando_alerta(pasta), 'timeout': 10}]})
        msgs.append('Notification: "needs you" alert added')
    return dados, msgs


def remover(dados, pasta=AQUI):
    """Tira so o que aponta para esta pasta. Devolve (dados, mensagens)."""
    msgs = []
    script_uso = os.path.join(pasta, 'hooks', 'status-uso.py')
    atual = dados.get('statusLine')
    cmd = atual.get('command', '') if isinstance(atual, dict) else ''
    if script_uso in cmd:
        original = original_encadeado(cmd)
        if original:
            dados['statusLine'] = dict(atual, command=original)
            msgs.append('statusLine: your original was restored')
        else:
            del dados['statusLine']
            msgs.append('statusLine: removed')
    script_alerta = os.path.join(pasta, 'hooks', 'notificacao-sessao.py')
    hooks = dados.get('hooks') if isinstance(dados.get('hooks'), dict) else {}
    lista = hooks.get('Notification')
    if isinstance(lista, list):
        novos = []
        for grupo in lista:
            if isinstance(grupo, dict) and isinstance(grupo.get('hooks'), list):
                grupo = dict(grupo, hooks=[h for h in grupo['hooks'] if script_alerta not in str(h.get('command', ''))])
                if not grupo['hooks']:
                    msgs.append('Notification: alert removed')
                    continue
            novos.append(grupo)
        if novos:
            hooks['Notification'] = novos
        else:
            hooks.pop('Notification', None)
        if not hooks and 'hooks' in dados:
            del dados['hooks']
    return dados, msgs


def main():
    parser = argparse.ArgumentParser(description='Agent Map hooks in the Claude Code settings.json')
    parser.add_argument('acao', choices=('install', 'uninstall', 'instalar', 'remover'))
    parser.add_argument('--settings', default=SETTINGS)
    args = parser.parse_args()
    try:
        antes = ler(args.settings)
        dados, msgs = (instalar if args.acao in ('install', 'instalar') else remover)(json.loads(json.dumps(antes)))
    except ValueError as e:
        sys.exit('settings.json: %s (nothing was changed)' % e)
    if dados != antes:
        gravar(args.settings, dados)
    for m in msgs:
        print('  ' + m)


if __name__ == '__main__':
    main()
