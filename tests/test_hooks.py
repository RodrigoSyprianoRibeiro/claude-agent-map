"""Testes dos hooks (alerta e uso) e do registro deles no settings.json."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, RAIZ)
import configurar_claude as cc  # noqa: E402

PASTA = '/opt/mapa agentes'  # com espaco: os comandos precisam sair citados


class TestConfigurarClaude(unittest.TestCase):
    def test_settings_vazio(self):
        dados, _ = cc.instalar({}, PASTA)
        self.assertEqual(dados['statusLine']['command'], "python3 '/opt/mapa agentes/hooks/status-uso.py'")
        self.assertEqual(dados['hooks']['Notification'][0]['hooks'][0]['command'], "python3 '/opt/mapa agentes/hooks/notificacao-sessao.py'")

    def test_idempotente(self):
        uma, _ = cc.instalar({}, PASTA)
        duas, _ = cc.instalar(json.loads(json.dumps(uma)), PASTA)
        self.assertEqual(uma, duas)

    def test_statusline_existente_e_encadeado_e_volta_na_remocao(self):
        antes = {'statusLine': {'type': 'command', 'command': "bash ~/minha linha.sh 'x'", 'padding': 1},
                 'hooks': {'Notification': [{'hooks': [{'type': 'command', 'command': 'outro.sh'}]}], 'Stop': []}}
        dados, _ = cc.instalar(json.loads(json.dumps(antes)), PASTA)
        self.assertEqual(cc.original_encadeado(dados['statusLine']['command']), "bash ~/minha linha.sh 'x'")
        self.assertEqual(dados['statusLine']['padding'], 1)
        self.assertEqual(len(dados['hooks']['Notification']), 2)
        depois, _ = cc.remover(dados, PASTA)
        self.assertEqual(depois, antes)

    def test_remover_do_zero_limpa_tudo(self):
        dados, _ = cc.instalar({'model': 'x'}, PASTA)
        self.assertEqual(cc.remover(dados, PASTA)[0], {'model': 'x'})

    def test_alerta_ja_registrado_por_outro_caminho_nao_duplica(self):
        antes = {'hooks': {'Notification': [{'hooks': [{'type': 'command', 'command': '~/.claude/hooks/notificacao-sessao.sh'}]}]}}
        dados, _ = cc.instalar(antes, PASTA)
        self.assertEqual(len(dados['hooks']['Notification']), 1)

    def test_grava_com_backup(self):
        pasta = tempfile.mkdtemp()
        arq = os.path.join(pasta, 'settings.json')
        with open(arq, 'w') as f:
            json.dump({'a': 1}, f)
        cc.gravar(arq, {'a': 2})
        cc.gravar(arq, {'a': 3})  # mesmo segundo: o primeiro backup (com o original) nao pode sumir
        with open(arq) as f:
            self.assertEqual(json.load(f), {'a': 3})
        backups = sorted(n for n in os.listdir(pasta) if n.startswith('settings.json.bak-mapa-agentes-'))
        self.assertEqual(len(backups), 2)
        with open(os.path.join(pasta, backups[0])) as f:
            self.assertEqual(json.load(f), {'a': 1})


class TestHookAlerta(unittest.TestCase):
    def rodar(self, entrada, home):
        # PATH so com o python: sem notify-send, nada aparece na tela durante os testes
        env = dict(os.environ, HOME=home, PATH=os.path.dirname(sys.executable))
        return subprocess.run([sys.executable, os.path.join(RAIZ, 'hooks', 'notificacao-sessao.py')], input=entrada,
                              capture_output=True, text=True, env=env, timeout=10)

    def test_grava_alerta_e_recusa_id_ruim(self):
        home = tempfile.mkdtemp()
        r = self.rodar(json.dumps({'session_id': 'abc-1', 'message': 'needs your permission', 'cwd': '/x'}), home)
        self.assertEqual(r.returncode, 0)
        with open(os.path.join(home, '.claude', 'state', 'atencao', 'abc-1.json')) as f:
            self.assertEqual(json.load(f)['tipo'], 'permission_prompt')
        self.assertEqual(self.rodar(json.dumps({'session_id': '../../x'}), home).returncode, 0)
        self.assertEqual(os.listdir(os.path.join(home, '.claude', 'state', 'atencao')), ['abc-1.json'])
        self.assertEqual(self.rodar('nao e json', home).returncode, 0)


if __name__ == '__main__':
    unittest.main()
