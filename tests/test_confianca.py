"""Fluxo de confianca: detector do dialogo, limpeza de transcrito vazio e a rota WS /ws/confiar."""

import asyncio
import json
import os
import sys
import tempfile
import unittest

from aiohttp import ClientSession
from aiohttp.test_utils import TestServer, unused_port

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import confianca  # noqa: E402
import servidor  # noqa: E402

DIALOGO = b'\x1b[2J\x1b[HAccessing workspace: /x\r\n Quick safety check: Is this a project you created or one you trust?\r\n \x1b[36m\xe2\x9d\xaf\x1b[39m No, exit\r\n   Yes, I trust this folder\r\n Enter to confirm \xc2\xb7 Esc to cancel'
NORMAL = b'\x1b[H Claude Code v2.1.296 \r\n \xe2\x9d\xaf Try "how do I log an error?"\r\n \xe2\x8f\xb5\xe2\x8f\xb5 auto mode on (shift+tab to cycle)'


class TestDetector(unittest.TestCase):
    def test_aceita_so_depois_do_dialogo_e_de_um_enter(self):
        d = confianca.Detector()
        self.assertEqual(d.saida(b'\x1b[?25l carregando'), 'aguardando')
        self.assertEqual(d.saida(DIALOGO), 'dialogo')
        self.assertEqual(d.saida(NORMAL), 'dialogo')  # tela normal sem resposta do usuario nao conta
        d.entrada(b'\x1b[B')  # seta para baixo: ainda nao respondeu
        self.assertEqual(d.saida(b'redesenho do dialogo Yes, I trust this folder'), 'dialogo')
        d.entrada(b'\r')
        self.assertEqual(d.saida(NORMAL), 'aceita')

    def test_marcadores_podem_chegar_em_pedacos(self):
        d = confianca.Detector()
        d.saida(DIALOGO)
        d.entrada(b'\r')
        self.assertEqual(d.saida(b'Claude Code v2 \xe2\x9d\xaf Tr'), 'dialogo')
        self.assertEqual(d.saida(b'y "how do I'), 'aceita')

    def test_sem_dialogo_nao_aceita_nada(self):
        d = confianca.Detector()
        d.entrada(b'\r')
        self.assertEqual(d.saida(NORMAL), 'aguardando')


class TestLimpeza(unittest.TestCase):
    def test_nome_da_pasta_do_projeto(self):
        p = confianca.pasta_do_projeto('/home/x/dev/.mapa-teste', base='/b')
        self.assertEqual(p, '/b/-home-x-dev--mapa-teste')

    def test_apaga_so_transcrito_novo_e_sem_prompt_de_usuario(self):
        proj = tempfile.mkdtemp()

        def criar(nome, linhas):
            with open(os.path.join(proj, nome + '.jsonl'), 'w') as f:
                for l in linhas:
                    f.write(json.dumps(l) + '\n')
            os.makedirs(os.path.join(proj, nome), exist_ok=True)

        criar('antigo-vazio', [{'type': 'queue-operation'}])
        antes = confianca.transcritos(proj)
        criar('novo-vazio', [{'type': 'queue-operation'}, {'type': 'user', 'isMeta': True, 'message': {'role': 'user', 'content': 'x'}}])
        criar('novo-com-prompt', [{'type': 'user', 'timestamp': '2026-10-10T10:00:00Z', 'message': {'role': 'user', 'content': [{'type': 'text', 'text': 'oi'}]}}])
        apagados = confianca.limpar_transcritos_vazios(proj, antes)
        self.assertEqual([os.path.basename(a) for a in apagados], ['novo-vazio.jsonl'])
        self.assertTrue(os.path.exists(os.path.join(proj, 'antigo-vazio.jsonl')))   # ja existia: nao e do claude de confianca
        self.assertTrue(os.path.exists(os.path.join(proj, 'novo-com-prompt.jsonl')))  # tem prompt: nunca apaga
        self.assertFalse(os.path.exists(os.path.join(proj, 'novo-vazio')))


TOKEN = 'tok'


class TerminalConfiancaFalso:
    def __init__(self):
        self.mestre, self.alimenta = os.pipe()
        self.escritos = []
        self.desanexado = False
        self.encerrado = False

    def escrever(self, dados):
        self.escritos.append(bytes(dados))

    def redimensionar(self, c, l):
        pass

    def desanexar(self):
        if self.encerrado:
            return
        self.encerrado = True
        self.desanexado = True
        for fd in (self.alimenta, self.mestre):
            try:
                os.close(fd)
            except OSError:
                pass
        self.mestre = -1


class EstadoFalso:
    def __init__(self, pasta):
        self.estado = {'nos': [{'id': 'empresa/proj', 'caminho': pasta}, {'id': 'empresa/sem', 'caminho': None}], 'sessoes': []}

    async def obter(self):
        return self.estado

    def invalidar(self):
        pass


class TestRotaConfiar(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.home = tempfile.mkdtemp()
        self.pasta = os.path.join(self.home, 'dev', 'proj')
        os.makedirs(self.pasta)
        self.porta = unused_port()
        self.fabricados = []
        self.pastas = []

        def fabrica(pasta, cols, linhas):
            self.pastas.append(pasta)
            f = TerminalConfiancaFalso()
            self.fabricados.append(f)
            return f

        app = servidor.criar_app(EstadoFalso(self.pasta), self.porta, TOKEN, home=self.home, fabrica_confianca=fabrica)
        self.srv = TestServer(app, host='127.0.0.1', port=self.porta)
        await self.srv.start_server()
        self.http = ClientSession()
        self.base = 'http://127.0.0.1:%d' % self.porta

    async def asyncTearDown(self):
        await self.http.close()
        await self.srv.close()

    async def conectar(self, query, origem='auto', host=None):
        h = {}
        if origem == 'auto':
            h['Origin'] = self.base
        elif origem:
            h['Origin'] = origem
        if host:
            h['Host'] = host
        try:
            return 101, await self.http.ws_connect(self.base + '/ws/confiar?' + query, headers=h)
        except Exception as e:
            return getattr(e, 'status', None), None

    async def test_recusas_antes_do_upgrade(self):
        casos = [('no=empresa/proj&token=errado', 'auto', None, 403), ('no=empresa/proj', 'auto', None, 403),
                 ('no=empresa/proj&token=%s' % TOKEN, 'https://evil.com', None, 403), ('no=empresa/proj&token=%s' % TOKEN, None, None, 403),
                 ('no=empresa/proj&token=%s' % TOKEN, 'auto', 'atacante.com:%d' % self.porta, 403),
                 ('no=nao/existe&token=%s' % TOKEN, 'auto', None, 404), ('no=empresa/sem&token=%s' % TOKEN, 'auto', None, 404),
                 ('token=%s' % TOKEN, 'auto', None, 404), ('no=../../etc&token=%s' % TOKEN, 'auto', None, 404)]
        for q, origem, host, esperado in casos:
            status, _ = await self.conectar(q, origem, host)
            self.assertEqual(status, esperado, q)
        self.assertEqual(self.fabricados, [])  # nada foi executado

    async def test_pasta_fora_da_home_e_recusada(self):
        servidor_estado = EstadoFalso('/tmp')
        porta = unused_port()
        srv = TestServer(servidor.criar_app(servidor_estado, porta, TOKEN, home=self.home, fabrica_confianca=lambda *a: self.fail('executou')),
                         host='127.0.0.1', port=porta)
        await srv.start_server()
        try:
            base = 'http://127.0.0.1:%d' % porta
            with self.assertRaises(Exception) as cm:
                await self.http.ws_connect(base + '/ws/confiar?no=empresa/proj&token=%s' % TOKEN, headers={'Origin': base})
            self.assertEqual(cm.exception.status, 400)
        finally:
            await srv.close()

    async def test_conta_no_limite_de_terminais(self):
        antigo = servidor.MAX_TERMINAIS
        servidor.MAX_TERMINAIS = 0
        try:
            status, _ = await self.conectar('no=empresa/proj&token=%s' % TOKEN)
        finally:
            servidor.MAX_TERMINAIS = antigo
        self.assertEqual(status, 429)

    async def test_aceita_encerra_o_claude_e_avisa(self):
        status, ws = await self.conectar('no=empresa/proj&token=%s' % TOKEN)
        self.assertEqual(status, 101)
        self.assertEqual(self.pastas, [os.path.realpath(self.pasta)])  # pasta vem do layout
        fake = self.fabricados[0]
        os.write(fake.alimenta, DIALOGO)
        msg = await ws.receive(timeout=3)
        self.assertIn(b'Quick safety check', msg.data)
        await ws.send_str('\x1b[B')
        await ws.send_str('\r')
        await asyncio.sleep(0.2)
        os.write(fake.alimenta, NORMAL)
        recebido = None
        for _ in range(5):
            m = await ws.receive(timeout=3)
            if m.type == 1:  # TEXT
                recebido = json.loads(m.data)
                break
        self.assertEqual(recebido, {'tipo': 'confianca', 'estado': 'aceita'})
        self.assertTrue(fake.desanexado)  # o claude interativo nao fica rodando
        self.assertEqual(fake.escritos, [b'\x1b[B', b'\r'])

    async def test_recusa_quando_o_claude_sai_sem_aceitar(self):
        status, ws = await self.conectar('no=empresa/proj&token=%s' % TOKEN)
        fake = self.fabricados[0]
        os.write(fake.alimenta, DIALOGO)
        await ws.receive(timeout=3)
        await ws.send_str('\r')  # default do dialogo e "No, exit"
        await asyncio.sleep(0.2)
        os.close(fake.alimenta)  # o claude saiu
        recebido = None
        for _ in range(5):
            m = await ws.receive(timeout=3)
            if m.type == 1:
                recebido = json.loads(m.data)
                break
        self.assertEqual(recebido, {'tipo': 'confianca', 'estado': 'recusada'})


if __name__ == '__main__':
    unittest.main()
