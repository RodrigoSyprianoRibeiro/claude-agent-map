"""Limpeza de sessoes criadas por testes: `claude rm` nao apaga o transcrito, e ele aparece na lista do VSCode.

Apaga SO o transcrito do id informado (e a pasta <id>/), e so se o customTitle comecar com `teste-`.
"""

import glob
import json
import os
import shutil
import subprocess


def titulo_do_transcrito(caminho):
    """Ultimo customTitle gravado no .jsonl, ou None."""
    titulo = None
    try:
        with open(caminho, errors='replace') as arq:
            for linha in arq:
                if '"custom-title"' not in linha:
                    continue
                try:
                    j = json.loads(linha)
                except ValueError:
                    continue
                if j.get('type') == 'custom-title' and j.get('customTitle'):
                    titulo = j['customTitle']
    except OSError:
        pass
    return titulo


def apagar_transcrito_de_teste(session_id, base=None):
    """True se apagou. Recusa id mal formado e qualquer sessao cujo titulo nao comece com `teste-`."""
    if not isinstance(session_id, str) or len(session_id) < 8 or not all(c.isalnum() or c == '-' for c in session_id):
        return False
    base = base or os.path.expanduser('~/.claude/projects')
    apagou = False
    for caminho in glob.glob(os.path.join(base, '*', session_id + '.jsonl')):
        titulo = titulo_do_transcrito(caminho)
        if not titulo or not titulo.startswith('teste-'):
            continue
        os.remove(caminho)
        pasta = caminho[:-6]
        if os.path.isdir(pasta):
            shutil.rmtree(pasta)
        apagou = True
    return apagou


def sessao_id_do_job(job):
    """sessionId de um job background (via claude agents --json), ou None."""
    try:
        r = subprocess.run(['claude', 'agents', '--json', '--all'], capture_output=True, text=True, timeout=60)
        return next((a.get('sessionId') for a in json.loads(r.stdout or '[]') if a.get('id') == job), None)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def remover_sessao_de_teste(job):
    """stop + rm do job e apaga o transcrito (so se o titulo comecar com `teste-`). Devolve o sessionId."""
    sid = sessao_id_do_job(job)
    subprocess.run(['claude', 'stop', job], capture_output=True, timeout=60)
    subprocess.run(['claude', 'rm', job], capture_output=True, timeout=60)
    if sid:
        apagar_transcrito_de_teste(sid)
    return sid
