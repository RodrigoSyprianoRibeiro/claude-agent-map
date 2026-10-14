"""Terminal embutido: `claude attach <jobId>` dentro de um PTY.

Executa codigo, entao o argv e FIXO: so o id (ja validado pelo servidor contra as sessoes
background abertas) entra no comando. Desanexar manda SIGHUP so ao grupo do processo attach:
a sessao --bg continua viva (nunca `claude stop`, nunca matar a sessao).
So biblioteca padrao.
"""

import fcntl
import os
import pty
import signal
import struct
import subprocess
import termios

import ambiente


def tamanho_valido(cols, linhas):
    try:
        return max(20, min(int(cols), 500)), max(5, min(int(linhas), 200))
    except (TypeError, ValueError):
        return 120, 30


class Terminal:
    def __init__(self, job_id, cols=120, linhas=30, comando=('claude', 'attach'), argv=None, cwd=None):
        """Por padrao `claude attach <job_id>`. `argv` (ja fixo e montado pelo servidor) e `cwd` servem ao terminal de confianca."""
        self.job_id = job_id
        cols, linhas = tamanho_valido(cols, linhas)
        self.mestre, escravo = pty.openpty()
        self._tamanho(cols, linhas)
        env = dict(ambiente.padrao.obter(), TERM='xterm-256color', COLORTERM='truecolor')
        try:
            self.proc = subprocess.Popen(
                list(argv) if argv else [*comando, job_id], stdin=escravo, stdout=escravo, stderr=escravo,
                start_new_session=True, close_fds=True, env=env, cwd=cwd or os.path.expanduser('~'))
        except Exception:
            os.close(self.mestre)
            raise
        finally:
            os.close(escravo)

    def _tamanho(self, cols, linhas):
        fcntl.ioctl(self.mestre, termios.TIOCSWINSZ, struct.pack('HHHH', linhas, cols, 0, 0))

    def redimensionar(self, cols, linhas):
        cols, linhas = tamanho_valido(cols, linhas)
        self._tamanho(cols, linhas)
        try:  # sem terminal controlador o kernel nao avisa: SIGWINCH explicito para o grupo
            os.killpg(self.proc.pid, signal.SIGWINCH)
        except ProcessLookupError:
            pass

    def escrever(self, dados):
        while dados:
            try:
                n = os.write(self.mestre, dados)
            except InterruptedError:
                continue
            dados = dados[n:]

    def desanexar(self):
        """SIGHUP no grupo do attach (o PGID e o PID por causa de start_new_session) e fecha o PTY."""
        if getattr(self, 'encerrado', False):  # ja desanexado: nao mexe em PID que pode ter sido reaproveitado
            return
        self.encerrado = True
        try:
            os.killpg(self.proc.pid, signal.SIGHUP)
        except ProcessLookupError:
            pass
        try:
            self.proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            for sinal in (signal.SIGTERM, signal.SIGKILL):
                try:
                    os.killpg(self.proc.pid, sinal)
                except ProcessLookupError:
                    break
                try:
                    self.proc.wait(timeout=2)
                    break
                except subprocess.TimeoutExpired:
                    continue
        try:
            os.close(self.mestre)
        except OSError:
            pass
        self.mestre = -1
