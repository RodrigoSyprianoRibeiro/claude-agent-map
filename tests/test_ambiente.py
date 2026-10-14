"""Testes do ambiente dos subprocessos claude: python3 -m unittest test_ambiente.py"""

import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import ambiente  # noqa: E402
import servidor  # noqa: E402

SAIDA = """LANG=pt_BR.UTF-8
PATH=/usr/bin:/bin
SSH_AUTH_SOCK=/run/user/1000/keyring/ssh
DISPLAY=:0
XAUTHORITY=/home/x/.Xauthority
DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
XDG_RUNTIME_DIR=/run/user/1000
LD_PRELOAD=/tmp/evil.so
WAYLAND_DISPLAY=
CLAUDE_CODE_OAUTH_TOKEN=$'segredo'
"""


class Relogio:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class TestAmbiente(unittest.TestCase):
    def test_so_as_chaves_permitidas_e_sem_valor_vazio(self):
        achadas = ambiente.interpretar(SAIDA)
        self.assertEqual(set(achadas), {'SSH_AUTH_SOCK', 'DISPLAY', 'XAUTHORITY', 'DBUS_SESSION_BUS_ADDRESS', 'XDG_RUNTIME_DIR'})
        self.assertEqual(achadas['DISPLAY'], ':0')

    def test_valor_entre_cifrao_aspas(self):
        self.assertEqual(ambiente.interpretar("DISPLAY=$':1'"), {'DISPLAY': ':1'})

    def test_sobrepoe_so_essas_chaves_no_env_do_subprocesso(self):
        a = ambiente.Ambiente(ler=lambda: SAIDA)
        base = {'PATH': '/meu/path', 'DISPLAY': ':antigo', 'LD_PRELOAD': '', 'HOME': '/home/x'}
        env = a.obter(base)
        self.assertEqual(env['DISPLAY'], ':0')                      # atualizado pelo systemd --user
        self.assertEqual(env['SSH_AUTH_SOCK'], '/run/user/1000/keyring/ssh')
        self.assertEqual(env['PATH'], '/meu/path')                   # nao-listadas ficam como estao
        self.assertEqual(env['LD_PRELOAD'], '')
        self.assertNotIn('CLAUDE_CODE_OAUTH_TOKEN', env)
        self.assertEqual(base['DISPLAY'], ':antigo')                 # o dict original nao e alterado

    def test_cache_de_30_segundos(self):
        chamadas, relogio = [], Relogio()

        def ler():
            chamadas.append(1)
            return 'DISPLAY=:%d' % len(chamadas)

        a = ambiente.Ambiente(ler=ler, relogio=relogio)
        self.assertEqual(a.obter({})['DISPLAY'], ':1')
        relogio.t += 29
        self.assertEqual(a.obter({})['DISPLAY'], ':1')
        self.assertEqual(len(chamadas), 1)
        relogio.t += 2
        self.assertEqual(a.obter({})['DISPLAY'], ':2')
        self.assertEqual(len(chamadas), 2)

    def test_falha_usa_o_ambiente_atual_e_tenta_de_novo_em_poucos_segundos(self):
        relogio, estado = Relogio(), {'quebrado': True}

        def ler():
            if estado['quebrado']:
                raise RuntimeError('sem systemd')
            return 'DISPLAY=:7'

        a = ambiente.Ambiente(ler=ler, relogio=relogio)
        self.assertEqual(a.obter({'DISPLAY': ':atual'})['DISPLAY'], ':atual')
        estado['quebrado'] = False
        relogio.t += 2
        self.assertEqual(a.obter({'DISPLAY': ':atual'})['DISPLAY'], ':atual')  # ainda no cache da falha
        relogio.t += 4
        self.assertEqual(a.obter({'DISPLAY': ':atual'})['DISPLAY'], ':7')

    def test_timeout_e_oserror_tambem_caem_no_ambiente_atual(self):
        for erro in (subprocess.TimeoutExpired('systemctl', 3), FileNotFoundError('systemctl')):
            def ler(e=erro):
                raise e
            self.assertEqual(ambiente.Ambiente(ler=ler).obter({'X': '1'}), {'X': '1'})

    def test_systemctl_real_usa_timeout_curto(self):
        with mock.patch('subprocess.run') as run:
            run.return_value = mock.Mock(returncode=0, stdout='DISPLAY=:0\n', stderr='')
            ambiente.ler_systemctl()
        self.assertEqual(run.call_args.args[0], ['systemctl', '--user', 'show-environment'])
        self.assertLessEqual(run.call_args.kwargs['timeout'], 5)


class TestClaudeRecebeOAmbiente(unittest.TestCase):
    ESCOPO = ['systemd-run', '--user', '--scope', '--quiet', '--collect', '--']

    def rodar(self, argv, resultados, com_systemd_run=True):
        novo = ambiente.Ambiente(ler=lambda: 'DISPLAY=:9\nSSH_AUTH_SOCK=/run/ssh')
        procura = '/usr/bin/systemd-run' if com_systemd_run else None
        with mock.patch.object(servidor.ambiente, 'padrao', novo), mock.patch('shutil.which', return_value=procura), \
                mock.patch('subprocess.run', side_effect=resultados) as run:
            try:
                saida = servidor.iniciar_claude(argv, '/tmp')
            except RuntimeError as e:
                saida = e
        return saida, run

    def teste_resultado(self, rc=0, out='ok', err=''):
        return mock.Mock(returncode=rc, stdout=out, stderr=err)

    def test_roda_dentro_de_um_scope_proprio_com_o_env_do_systemd(self):
        saida, run = self.rodar(['claude', 'stop', 'abcd1234'], [self.teste_resultado()])
        self.assertEqual(saida, 'ok')
        self.assertEqual(run.call_args.args[0], self.ESCOPO + ['claude', 'stop', 'abcd1234'])
        env = run.call_args.kwargs['env']
        self.assertEqual((env['DISPLAY'], env['SSH_AUTH_SOCK']), (':9', '/run/ssh'))

    def test_sem_systemd_run_usa_a_execucao_direta(self):
        _, run = self.rodar(['claude', '--bg'], [self.teste_resultado()], com_systemd_run=False)
        self.assertEqual(run.call_args.args[0], ['claude', '--bg'])

    def test_falha_do_proprio_systemd_run_cai_na_execucao_direta(self):
        _, run = self.rodar(['claude', 'stop', 'abcd1234'], [
            self.teste_resultado(1, '', 'Failed to connect to bus: No medium found'), self.teste_resultado(0, 'parado')])
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args.args[0], ['claude', 'stop', 'abcd1234'])

    def test_erro_do_proprio_claude_nao_roda_de_novo(self):
        saida, run = self.rodar(['claude', '--bg', '--', 'oi'], [self.teste_resultado(1, '', 'erro: pasta nao confiavel')])
        self.assertEqual(run.call_count, 1)  # rodar duas vezes duplicaria a sessao
        self.assertIsInstance(saida, RuntimeError)

    def test_terminal_attach_tambem_usa(self):
        import terminal
        novo = ambiente.Ambiente(ler=lambda: 'DISPLAY=:9')
        with mock.patch.object(terminal.ambiente, 'padrao', novo), mock.patch('subprocess.Popen') as popen, \
                mock.patch('pty.openpty', return_value=(os.open(os.devnull, os.O_RDWR), os.open(os.devnull, os.O_RDWR))), \
                mock.patch('fcntl.ioctl'):
            terminal.Terminal('abcd1234', 80, 24)
        env = popen.call_args.kwargs['env']
        self.assertEqual((env['DISPLAY'], env['TERM']), (':9', 'xterm-256color'))
        self.assertEqual(popen.call_args.args[0], ['claude', 'attach', 'abcd1234'])


if __name__ == '__main__':
    unittest.main()
