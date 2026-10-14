"""Testes do uso do plano: calculo, rota /api/uso e o script do statusLine."""

import json
import os
import subprocess
import sys
import tempfile
import unittest

from aiohttp import ClientSession
from aiohttp.test_utils import TestServer, unused_port

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import servidor  # noqa: E402
import uso  # noqa: E402

HOOK = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'hooks', 'status-uso.py')
AGORA = 1_000_000.0


class TestCalculo(unittest.TestCase):
    def test_janela_com_reset_no_futuro(self):
        r = uso.calcular({'five_hour': {'used_percentage': 3.0, 'resets_at': AGORA + 900},
                          'seven_day': {'used_percentage': 10, 'resets_at': AGORA + 9 * 3600}, 'atualizadoEm': AGORA - 120}, AGORA)
        self.assertEqual((r['five_hour']['usado'], r['five_hour']['resetsEm'], r['five_hour']['zerada']), (3.0, 900, False))
        self.assertEqual(r['seven_day']['resetsEm'], 9 * 3600)
        self.assertEqual((r['idade'], r['velho'], r['disponivel']), (120, False, True))

    def test_reset_passado_zera_a_janela(self):
        r = uso.calcular({'five_hour': {'used_percentage': 97, 'resets_at': AGORA - 1},
                          'seven_day': {'used_percentage': 50, 'resets_at': AGORA + 100}, 'atualizadoEm': AGORA - 10}, AGORA)
        self.assertEqual((r['five_hour']['usado'], r['five_hour']['resetsEm'], r['five_hour']['zerada']), (0.0, None, True))
        self.assertEqual(r['seven_day']['usado'], 50.0)

    def test_dado_velho_e_janela_ausente(self):
        r = uso.calcular({'seven_day': {'used_percentage': 5, 'resets_at': AGORA + 5}, 'atualizadoEm': AGORA - 31 * 60}, AGORA)
        self.assertNotIn('five_hour', r)
        self.assertTrue(r['velho'])

    def test_sem_arquivo_ou_sem_janelas(self):
        self.assertEqual(uso.calcular(None), {'disponivel': False})
        self.assertFalse(uso.calcular({'atualizadoEm': AGORA}, AGORA)['disponivel'])
        self.assertIsNone(uso.ler('/nao/existe.json'))

    def test_spend_limit_mantem_os_dolares(self):
        r = uso.calcular({'spend_limit': {'used_percentage': 60, 'resets_at': AGORA + 50, 'used_usd': 12, 'limit_usd': 20, 'period': 'weekly'}, 'atualizadoEm': AGORA}, AGORA)
        self.assertEqual((r['spend_limit']['used_usd'], r['spend_limit']['period']), (12, 'weekly'))


class TestRota(unittest.IsolatedAsyncioTestCase):
    async def test_api_uso_exige_host_local_e_devolve_o_calculado(self):
        pasta = tempfile.mkdtemp()
        arq = os.path.join(pasta, 'uso.json')
        with open(arq, 'w') as f:
            json.dump({'five_hour': {'used_percentage': 3, 'resets_at': 4_000_000_000}, 'atualizadoEm': 4_000_000_000 - 60}, f)
        porta = unused_port()
        srv = TestServer(servidor.criar_app(object(), porta, 't', arquivo_uso=arq), host='127.0.0.1', port=porta)
        await srv.start_server()
        try:
            async with ClientSession() as c:
                async with c.get('http://127.0.0.1:%d/api/uso' % porta) as r:
                    j = await r.json()
                    self.assertEqual((r.status, j['five_hour']['usado'], j['disponivel']), (200, 3.0, True))
                async with c.get('http://127.0.0.1:%d/api/uso' % porta, headers={'Host': 'atacante.com:%d' % porta}) as r:
                    self.assertEqual(r.status, 403)
            os.remove(arq)
            async with ClientSession() as c:
                async with c.get('http://127.0.0.1:%d/api/uso' % porta) as r:
                    self.assertEqual(await r.json(), {'disponivel': False})
        finally:
            await srv.close()


@unittest.skipUnless(os.path.exists(HOOK), 'hook status-uso.py nao instalado')
class TestScriptDoStatusLine(unittest.TestCase):
    def setUp(self):
        self.estado = os.path.join(tempfile.mkdtemp(), 'estado', 'uso-plano.json')
        self.env = dict(os.environ, STATUS_USO_ESTADO=self.estado)

    def rodar(self, entrada):
        r = subprocess.run([sys.executable, HOOK], input=entrada, capture_output=True, text=True, env=self.env, timeout=10)
        self.assertEqual(r.returncode, 0)
        return r.stdout.strip()

    def test_exemplo_da_doc_grava_e_imprime(self):
        saida = self.rodar(json.dumps({'session_id': 's1', 'model': {'display_name': 'Opus'}, 'context_window': {'used_percentage': 8},
                                       'rate_limits': {'five_hour': {'used_percentage': 23.5, 'resets_at': 1738425600},
                                                       'seven_day': {'used_percentage': 41.2, 'resets_at': 1738857600}}}))
        self.assertEqual(saida, 'Opus · ctx 8% · 5h 24% · 7d 41%')
        d = json.load(open(self.estado))
        self.assertEqual((d['five_hour']['used_percentage'], d['sessao']), (23.5, 's1'))
        self.assertGreater(d['atualizadoEm'], 0)

    def test_janela_ausente_nao_apaga_a_anterior(self):
        self.rodar(json.dumps({'rate_limits': {'five_hour': {'used_percentage': 10, 'resets_at': 1}, 'seven_day': {'used_percentage': 20, 'resets_at': 2}}}))
        saida = self.rodar(json.dumps({'model': {'display_name': 'Haiku'}, 'rate_limits': {'five_hour': {'used_percentage': 12, 'resets_at': 3}}}))
        self.assertEqual(saida, 'Haiku · 5h 12%')
        d = json.load(open(self.estado))
        self.assertEqual((d['five_hour']['used_percentage'], d['seven_day']['used_percentage']), (12, 20))

    def test_sem_rate_limits_nao_cria_arquivo(self):
        self.assertEqual(self.rodar(json.dumps({'model': {'display_name': 'Opus'}})), 'Opus')
        self.assertFalse(os.path.exists(self.estado))

    def test_nunca_falha(self):
        for lixo in ('', 'lixo', '[]', '{"rate_limits": 5}', '{"rate_limits": {"five_hour": "x"}, "model": 3}'):
            self.rodar(lixo)
        self.env['STATUS_USO_ESTADO'] = '/proc/nao/pode/gravar.json'
        self.assertEqual(self.rodar(json.dumps({'model': {'display_name': 'X'}, 'rate_limits': {'five_hour': {'used_percentage': 1}}})), 'X · 5h 1%')


if __name__ == '__main__':
    unittest.main()
