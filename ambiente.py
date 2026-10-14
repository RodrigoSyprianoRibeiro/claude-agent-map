"""Ambiente dos subprocessos `claude`.

O servico systemd --user pode subir antes de a sessao grafica exportar SSH_AUTH_SOCK, DISPLAY etc.
para o systemd --user (depois de um reboot). Entao, na hora de rodar qualquer `claude`, le o
ambiente ATUAL do gerenciador (`systemctl --user show-environment`, cache curto) e sobrepoe so
estas chaves no env do subprocesso. Se o comando falhar, usa o ambiente do proprio processo.
So biblioteca padrao.
"""

import os
import subprocess
import time

CHAVES = ('SSH_AUTH_SOCK', 'DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS', 'WAYLAND_DISPLAY', 'XDG_RUNTIME_DIR')
TTL_SEGUNDOS = 30
TTL_FALHA_SEGUNDOS = 5
TIMEOUT_SEGUNDOS = 3


def ler_systemctl():
    """Saida de `systemctl --user show-environment`; levanta em caso de falha ou timeout."""
    r = subprocess.run(['systemctl', '--user', 'show-environment'], capture_output=True, text=True,
                       timeout=TIMEOUT_SEGUNDOS, stdin=subprocess.DEVNULL)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or 'systemctl saiu com codigo %d' % r.returncode)
    return r.stdout


def interpretar(saida):
    """{chave: valor} so das CHAVES permitidas. Aceita o valor entre $'...' que o systemd usa com caracteres especiais."""
    achadas = {}
    for linha in (saida or '').splitlines():
        chave, igual, valor = linha.partition('=')
        if not igual or chave not in CHAVES:
            continue
        if valor.startswith("$'") and valor.endswith("'") and len(valor) >= 3:
            valor = valor[2:-1]
        if valor and '\0' not in valor and '\n' not in valor:
            achadas[chave] = valor
    return achadas


class Ambiente:
    def __init__(self, ler=ler_systemctl, relogio=time.monotonic):
        self.ler = ler
        self.relogio = relogio
        self.cache = None       # {chave: valor} sobrepostas
        self.valido_ate = 0.0

    def sobrepostas(self):
        agora = self.relogio()
        if self.cache is None or agora >= self.valido_ate:
            try:
                self.cache = interpretar(self.ler())
                self.valido_ate = agora + TTL_SEGUNDOS
            except (OSError, RuntimeError, subprocess.SubprocessError):
                self.cache = {}  # falhou: ambiente atual; tenta de novo em poucos segundos
                self.valido_ate = agora + TTL_FALHA_SEGUNDOS
        return self.cache

    def obter(self, base=None):
        """Copia do ambiente (os.environ por padrao) com as chaves do systemd --user por cima."""
        env = dict(os.environ if base is None else base)
        env.update(self.sobrepostas())
        return env


padrao = Ambiente()
