#!/usr/bin/env python3
"""Hook Notification do Claude Code: registra que a sessao precisa de voce e avisa na tela.

Grava ~/.claude/state/atencao/<session_id>.json, que o Mapa de agentes le para pintar a sessao de
vermelho ("precisa de voce"). O alerta vale ate o transcrito ser escrito de novo (ferramenta aprovada
ou prompt novo), entao nao precisa de hook para limpar.

So o permission_prompt vira notificacao na tela (notify-send, se existir): o idle_prompt dispara em
toda aba parada ha 60 s e viraria barulho. Nunca falha: hook com erro atrapalha a sessao.
So biblioteca padrao.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

PASTA = os.path.expanduser('~/.claude/state/atencao')
ID = re.compile(r'^[0-9A-Za-z_-]{1,80}$')


def titulo_da_sessao(transcrito, cwd):
    """Nome dado a aba, senao o titulo gerado, senao a pasta: para saber qual aba e."""
    try:
        with open(transcrito, 'rb') as arq:
            arq.seek(max(0, os.path.getsize(transcrito) - 300_000))
            cauda = arq.read().decode('utf-8', 'replace')
    except (OSError, TypeError):
        cauda = ''
    for chave in ('customTitle', 'aiTitle'):
        achados = re.findall(r'"%s":"([^"]*)"' % chave, cauda)
        if achados:
            return achados[-1]
    return os.path.basename(cwd or '') or 'sessão'


def main():
    dados = json.load(sys.stdin)
    sessao = str(dados.get('session_id') or '')
    if not ID.match(sessao):
        return
    mensagem = str(dados.get('message') or '')
    tipo = dados.get('notification_type')
    if not tipo:  # versoes antigas: deduz pelo texto
        tipo = 'permission_prompt' if re.search(r'permiss', mensagem, re.I) else 'idle_prompt'
    os.makedirs(PASTA, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=PASTA, suffix='.tmp')
    with os.fdopen(fd, 'w') as arq:
        json.dump({'ts': int(time.time()), 'tipo': tipo, 'mensagem': mensagem, 'cwd': dados.get('cwd') or ''}, arq)
    os.replace(tmp, os.path.join(PASTA, sessao + '.json'))

    if tipo == 'permission_prompt' and shutil.which('notify-send'):
        titulo = titulo_da_sessao(dados.get('transcript_path'), dados.get('cwd'))
        subprocess.Popen(['timeout', '5', 'notify-send', '-a', 'Claude Code', '-u', 'normal', 'Claude: ' + titulo,
                          mensagem or 'Aguardando permissão'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
