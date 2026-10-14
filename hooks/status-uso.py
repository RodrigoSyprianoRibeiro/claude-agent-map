#!/usr/bin/env python3
"""statusLine do Claude Code: grava o uso do plano (5h e 7d) e imprime uma linha curta.

Recebe o JSON da sessao no stdin. Se vier `rate_limits`, mescla as janelas presentes em
~/.claude/state/uso-plano.json (atomico: tmp + rename; janela ausente nao apaga a anterior).
O app "Mapa de agentes" le esse arquivo. Nunca falha: qualquer excecao imprime so o que der e sai 0.
So biblioteca padrao. STATUS_USO_ESTADO muda o caminho do arquivo (testes).

`--encadear '<comando>'`: quem ja tinha um statusLine continua vendo a linha dele. O uso e gravado
aqui e o comando original recebe o mesmo JSON no stdin; a saida dele e o que aparece.
"""

import json
import os
import subprocess
import sys
import tempfile
import time

ESTADO = os.environ.get('STATUS_USO_ESTADO') or os.path.expanduser('~/.claude/state/uso-plano.json')
JANELAS = ('five_hour', 'seven_day', 'spend_limit')


def numero(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def janela_valida(j):
    return isinstance(j, dict) and numero(j.get('used_percentage')) is not None


def gravar(rate_limits, sessao):
    try:
        with open(ESTADO) as arq:
            atual = json.load(arq)
        if not isinstance(atual, dict):
            atual = {}
    except (OSError, ValueError):
        atual = {}
    mudou = False
    for nome in JANELAS:
        j = rate_limits.get(nome)
        if janela_valida(j):
            atual[nome] = {k: v for k, v in j.items() if isinstance(v, (int, float, str)) and not isinstance(v, bool)}
            mudou = True
    if not mudou:
        return
    atual['atualizadoEm'] = time.time()
    if sessao:
        atual['sessao'] = str(sessao)
    pasta = os.path.dirname(ESTADO)
    os.makedirs(pasta, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=pasta, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as arq:
            json.dump(atual, arq)
        os.replace(tmp, ESTADO)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def pct(v):
    v = numero(v)
    return None if v is None else '%d%%' % round(v)


def encadeado(argv):
    """Comando original passado em --encadear, ou None."""
    if '--encadear' in argv:
        i = argv.index('--encadear')
        return argv[i + 1] if i + 1 < len(argv) else None
    return None


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    bruto = sys.stdin.read()
    original = encadeado(argv)
    partes = []
    try:
        dados = json.loads(bruto)
        if not isinstance(dados, dict):
            dados = {}
        modelo = (dados.get('model') or {}).get('display_name') or (dados.get('model') or {}).get('id')
        if modelo:
            partes.append(str(modelo))
        ctx = pct((dados.get('context_window') or {}).get('used_percentage'))
        if ctx:
            partes.append('ctx ' + ctx)
        limites = dados.get('rate_limits')
        if isinstance(limites, dict):
            for nome, rotulo in (('five_hour', '5h'), ('seven_day', '7d')):
                p = pct((limites.get(nome) or {}).get('used_percentage')) if isinstance(limites.get(nome), dict) else None
                if p:
                    partes.append('%s %s' % (rotulo, p))
            try:
                gravar(limites, dados.get('session_id'))
            except Exception:
                pass
    except Exception:
        pass
    if original:
        try:
            r = subprocess.run(['/bin/sh', '-c', original], input=bruto, capture_output=True, text=True, timeout=10)
            sys.stdout.write(r.stdout)
            return
        except Exception:
            pass
    print(' · '.join(partes))


if __name__ == '__main__':
    try:
        main()
    except Exception:
        pass
    sys.exit(0)
