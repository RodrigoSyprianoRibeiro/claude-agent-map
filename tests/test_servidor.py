"""Testes das rotas do servidor (validacao de WS e POSTs), sem executar claude de verdade."""

import asyncio
import json
import os
import sys
import tempfile
import unittest

from aiohttp import ClientSession, WSMsgType
from aiohttp.test_utils import TestServer, unused_port

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import anexos  # noqa: E402
import servidor  # noqa: E402

TOKEN = 'tok-secreto'
JOB = 'ab12cd34'
SID_ABERTA = '11111111-1111-1111-1111-111111111111'
SID_FECHADA = '22222222-2222-2222-2222-222222222222'
SID_BG = '33333333-3333-3333-3333-333333333333'


class EstadoFalso:
    def __init__(self, pasta):
        self.pasta = pasta
        self.invalidacoes = 0
        self.estado = {
            'nos': [{'id': 'empresa/geral', 'caminho': pasta}, {'id': 'empresa/sem-pasta', 'caminho': None}],
            'sessoes': [
                {'id': SID_ABERTA, 'aberta': True, 'tipo': 'interactive', 'jobId': None, 'raiz': pasta, 'cwd': pasta},
                {'id': SID_FECHADA, 'aberta': False, 'tipo': None, 'jobId': None, 'raiz': pasta, 'cwd': pasta},
                {'id': SID_BG, 'aberta': True, 'tipo': 'bg', 'jobId': JOB, 'raiz': pasta, 'cwd': pasta},
            ]}

    async def obter(self):
        return self.estado

    @property
    def detalhes(self):
        pasta = self.pasta

        class Detalhes:
            def sessao(_, sid):
                return {'id': sid, 'subagentes': []} if sid in (SID_ABERTA, SID_FECHADA, SID_BG) else None

            def localizar(_, sid):
                caminho = os.path.join(pasta, sid + '.jsonl')
                return caminho if os.path.exists(caminho) else None
        return Detalhes()

    def invalidar(self):
        self.invalidacoes += 1


class TerminalFalso:
    """Sem processo: guarda o que o servidor escreveria no PTY."""

    def __init__(self):
        self.mestre, self._escrita = os.pipe()
        self.escritos = []
        self.desanexado = False

    def escrever(self, dados):
        self.escritos.append(bytes(dados))

    def redimensionar(self, cols, linhas):
        pass

    def desanexar(self):
        self.desanexado = True
        for fd in (self._escrita,):
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.close(self.mestre)
        except OSError:
            pass


PNG = b'\x89PNG\r\n\x1a\n' + b'\x00' * 32


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.home = tempfile.mkdtemp()
        self.pasta = os.path.join(self.home, 'dev', 'empresa')
        os.makedirs(self.pasta)
        self.chamadas = []
        self.saida = 'backgrounded · %s · loja' % JOB
        self.porta = unused_port()
        estado = EstadoFalso(self.pasta)
        self.estado = estado

        self.falha = None

        def executar(argv, pasta):
            self.chamadas.append((argv, pasta))
            if self.falha:
                raise RuntimeError(self.falha % pasta)
            return self.saida

        self.pasta_anexos = os.path.join(self.home, 'anexos')
        self.terminais_falsos = []

        def fabrica(job, cols, linhas):
            f = TerminalFalso()
            self.terminais_falsos.append(f)
            return f

        servidor.ESPERA_IMAGEM_MAX = 0.05
        servidor.PAUSA_APOS_IMAGEM = 0.01
        servidor.PAUSA_ANTES_ENTER = 0.01
        app = servidor.criar_app(estado, self.porta, TOKEN, executar=executar, home=self.home,
                                 fabrica_terminal=fabrica, pasta_anexos=self.pasta_anexos)
        self.servidor = TestServer(app, host='127.0.0.1', port=self.porta)
        await self.servidor.start_server()
        self.sessao = ClientSession()
        self.base = 'http://127.0.0.1:%d' % self.porta

    async def asyncTearDown(self):
        await self.sessao.close()
        await self.servidor.close()

    def cab(self, **extra):
        h = {'Origin': self.base, 'X-Token': TOKEN, 'Content-Type': 'application/json'}
        h.update(extra)
        return {k: v for k, v in h.items() if v is not None}

    async def post(self, rota, corpo, **cab):
        async with self.sessao.post(self.base + rota, json=corpo, headers=self.cab(**cab)) as r:
            return r.status, await r.json(content_type=None) if r.content_type == 'application/json' else await r.text()


class TestChrome(unittest.TestCase):
    def test_argv_do_app_com_perfil_e_classe_proprios(self):
        argv = servidor.argv_do_chrome('http://127.0.0.1:8767/?foco=empresa')
        self.assertEqual(argv[:3], ['google-chrome', '--app=http://127.0.0.1:8767/?foco=empresa', '--class=mapa-agentes'])
        self.assertTrue(argv[3].startswith('--user-data-dir=') and argv[3].endswith('/.config/mapa-agentes/chrome'))
        self.assertEqual(servidor.url_da_pagina(8767, 'empresa/portal-vagas'), 'http://127.0.0.1:8767/?foco=empresa/portal-vagas')
        self.assertEqual(servidor.url_da_pagina(8767, ''), 'http://127.0.0.1:8767/')


class TestExtrairJob(unittest.TestCase):
    def test_formatos(self):
        self.assertEqual(servidor.extrair_job_id('backgrounded · ab12cd34 · nome'), 'ab12cd34')
        self.assertEqual(servidor.extrair_job_id('x\nbackgrounded - zz99yy88\n'), 'zz99yy88')
        self.assertIsNone(servidor.extrair_job_id('nada aqui'))
        self.assertIsNone(servidor.extrair_job_id(None))


class TestPostsComuns(Base):
    async def test_post_sem_token_origem_errada_ou_host_errado(self):
        for rota in ('/api/nova-sessao', '/api/assumir', '/api/parar', '/api/posicao'):
            self.assertEqual((await self.post(rota, {}, **{'X-Token': None}))[0], 403, rota)
            self.assertEqual((await self.post(rota, {}, **{'X-Token': 'errado'}))[0], 403, rota)
            self.assertEqual((await self.post(rota, {}, Origin='https://evil.com'))[0], 403, rota)
            self.assertEqual((await self.post(rota, {}, Host='atacante.com:%d' % self.porta))[0], 403, rota)
        self.assertEqual(self.chamadas, [])


class TestNovaSessao(Base):
    async def test_cria_na_pasta_do_no_e_devolve_job(self):
        st, j = await self.post('/api/nova-sessao', {'no': 'empresa/geral', 'prompt': 'Responda apenas: ok', 'nome': 'teste'})
        self.assertEqual((st, j['jobId']), (200, JOB))
        argv, pasta = self.chamadas[0]
        self.assertEqual(argv, ['claude', '--bg', '--name', 'teste', '--', 'Responda apenas: ok'])
        self.assertEqual(pasta, os.path.realpath(self.pasta))

    async def test_prompt_opcional(self):
        st, _ = await self.post('/api/nova-sessao', {'no': 'empresa/geral'})
        self.assertEqual(st, 200)
        self.assertEqual(self.chamadas[0][0], ['claude', '--bg'])

    async def test_cliente_nao_escolhe_pasta(self):
        st, _ = await self.post('/api/nova-sessao', {'no': 'empresa/geral', 'cwd': '/etc', 'prompt': 'x'})
        self.assertEqual(st, 200)
        self.assertEqual(self.chamadas[0][1], os.path.realpath(self.pasta))

    async def test_id_de_regiao_inteira_usa_o_geral_ou_o_primeiro_no_com_pasta(self):
        self.estado.estado['nos'] = [
            {'id': 'empresa/zeta', 'regiao': 'empresa', 'caminho': self.pasta}, {'id': 'empresa/geral', 'regiao': 'empresa', 'caminho': None},
            {'id': 'bulk/geral', 'regiao': 'bulk', 'caminho': self.pasta}]
        st, _ = await self.post('/api/nova-sessao', {'no': 'empresa', 'prompt': 'x'})  # geral sem pasta: cai no primeiro com pasta
        self.assertEqual(st, 200)
        st, _ = await self.post('/api/nova-sessao', {'no': 'bulk', 'prompt': 'x'})
        self.assertEqual(st, 200)
        self.assertEqual((await self.post('/api/nova-sessao', {'no': 'inexistente', 'prompt': 'x'}))[0], 400)
        self.assertEqual(len(self.chamadas), 2)

    async def test_no_inexistente_sem_pasta_ou_fora_da_home(self):
        self.assertEqual((await self.post('/api/nova-sessao', {'no': 'empresa/fantasma', 'prompt': 'x'}))[0], 400)
        self.assertEqual((await self.post('/api/nova-sessao', {'no': 'empresa/sem-pasta', 'prompt': 'x'}))[0], 400)
        self.assertEqual((await self.post('/api/nova-sessao', {'no': 7}))[0], 400)
        self.estado.estado['nos'][0]['caminho'] = '/tmp'
        self.assertEqual((await self.post('/api/nova-sessao', {'no': 'empresa/geral', 'prompt': 'x'}))[0], 400)
        self.assertEqual(self.chamadas, [])

    async def test_pasta_nao_confiavel_devolve_codigo_para_o_botao_de_confianca(self):
        self.falha = 'Workspace not trusted. Run `claude` in %s once and accept the trust prompt, then retry.'
        st, j = await self.post('/api/nova-sessao', {'no': 'empresa/geral', 'prompt': 'x'})
        self.assertEqual((st, j['codigo'], j['no']), (400, 'nao_confiavel', 'empresa/geral'))
        self.assertIn('not trusted', j['erro'])

    async def test_id_ausente_na_saida_vira_502(self):
        self.saida = 'sem id'
        st, _ = await self.post('/api/nova-sessao', {'no': 'empresa/geral', 'prompt': 'x'})
        self.assertEqual(st, 502)


class TestAssumir(Base):
    async def test_sessao_aberta_no_vscode_da_409(self):
        st, j = await self.post('/api/assumir', {'id': SID_ABERTA})
        self.assertEqual(st, 409)
        self.assertEqual(j['erro'], 'feche a aba no VSCode antes de assumir aqui')
        self.assertEqual(self.chamadas, [])

    async def test_fechada_resume_em_background(self):
        st, j = await self.post('/api/assumir', {'id': SID_FECHADA})
        self.assertEqual((st, j['jobId']), (200, JOB))
        self.assertEqual(self.chamadas[0][0], ['claude', '--bg', '--resume', SID_FECHADA])
        self.assertEqual(self.chamadas[0][1], os.path.realpath(self.pasta))

    async def test_inexistente_e_id_invalido(self):
        self.assertEqual((await self.post('/api/assumir', {'id': '99999999-9999-9999-9999-999999999999'}))[0], 404)
        self.assertEqual((await self.post('/api/assumir', {'id': '--evil'}))[0], 400)
        self.assertEqual((await self.post('/api/assumir', {}))[0], 400)

    async def test_background_aberta_tambem_nao_assume(self):
        self.assertEqual((await self.post('/api/assumir', {'id': SID_BG}))[0], 409)


class TestParar(Base):
    async def test_para_so_sessao_background_existente(self):
        self.assertEqual((await self.post('/api/parar', {'id': JOB}))[0], 200)
        self.assertEqual(self.chamadas[0][0], ['claude', 'stop', JOB])
        self.assertEqual((await self.post('/api/parar', {'id': 'zz99yy88'}))[0], 404)
        self.assertEqual((await self.post('/api/parar', {'id': SID_ABERTA}))[0], 404)  # interativa: nao e job
        self.assertEqual((await self.post('/api/parar', {'id': '-rf'}))[0], 400)
        self.assertEqual(len(self.chamadas), 1)


class TestNovaSessaoModelo(Base):
    async def test_modelo_e_esforco_entram_antes_do_nome_e_do_separador(self):
        st, _ = await self.post('/api/nova-sessao', {'no': 'empresa/geral', 'prompt': 'oi', 'nome': 'n', 'modelo': 'haiku', 'esforco': 'low'})
        self.assertEqual(st, 200)
        self.assertEqual(self.chamadas[0][0], ['claude', '--bg', '--model', 'haiku', '--effort', 'low', '--name', 'n', '--', 'oi'])

    async def test_sessao_instantanea_sem_nome_e_sem_prompt_com_opus_high(self):
        st, j = await self.post('/api/nova-sessao', {'no': 'empresa/geral', 'modelo': 'opus', 'esforco': 'high'})
        self.assertEqual((st, j['jobId']), (200, JOB))
        self.assertEqual(self.chamadas[0][0], ['claude', '--bg', '--model', 'opus', '--effort', 'high'])

    async def test_fora_da_lista_branca_e_recusado(self):
        for corpo in ({'modelo': 'gpt-9'}, {'esforco': 'turbo'}, {'modelo': '--evil'}):
            st, _ = await self.post('/api/nova-sessao', dict({'no': 'empresa/geral', 'prompt': 'oi'}, **corpo))
            self.assertEqual(st, 400, corpo)
        self.assertEqual(self.chamadas, [])


class TestConfigDaSessao(Base):
    async def detalhe(self, sid=SID_BG):
        async with self.sessao.get('%s/api/sessao/%s' % (self.base, sid)) as r:
            return r.status, await r.json()

    async def test_detalhe_devolve_modelo_e_esforco_da_criacao(self):
        st, j = await self.post('/api/nova-sessao', {'no': 'empresa/geral', 'prompt': 'x', 'modelo': 'haiku', 'esforco': 'low'})
        self.assertEqual(j['jobId'], JOB)  # o fake sempre devolve o mesmo job, que e o da sessao SID_BG
        st, d = await self.detalhe()
        self.assertEqual((st, d['config']), (200, {'modelo': 'haiku', 'esforco': 'low'}))

    async def test_sem_configuracao_conhecida_vem_vazio(self):
        self.assertEqual((await self.detalhe())[1]['config'], {})
        self.assertEqual((await self.detalhe(SID_ABERTA))[1]['config'], {})  # sem jobId

    async def test_ultimo_comando_pelo_ws_atualiza_a_configuracao(self):
        await self.post('/api/nova-sessao', {'no': 'empresa/geral', 'prompt': 'x', 'modelo': 'haiku', 'esforco': 'low'})
        ws = await self.sessao.ws_connect('%s/ws/terminal?id=%s&token=%s' % (self.base, JOB, TOKEN), headers={'Origin': self.base})
        await ws.send_json({'tipo': 'comando', 'nome': 'effort', 'valor': 'high'})
        await ws.send_json({'tipo': 'comando', 'nome': 'model', 'valor': 'turbo'})  # fora da lista branca: nao grava
        await ws.send_json({'tipo': 'comando', 'nome': 'model', 'valor': 'opus'})
        await asyncio.sleep(0.4)
        await ws.close()
        self.assertEqual((await self.detalhe())[1]['config'], {'modelo': 'opus', 'esforco': 'high'})


class TestAnexo(Base):
    async def enviar(self, corpo, tipo='image/png', **cab):
        h = self.cab(**{'Content-Type': tipo}, **cab)
        async with self.sessao.post(self.base + '/api/anexo', data=corpo, headers=h) as r:
            corpo = await r.text()
            try:
                return r.status, json.loads(corpo)
            except ValueError:
                return r.status, corpo

    async def test_upload_valido_devolve_so_o_id_e_grava_com_permissao_restrita(self):
        st, j = await self.enviar(PNG)
        self.assertEqual(st, 200)
        self.assertEqual(list(j), ['id'])
        self.assertRegex(j['id'], r'^[0-9a-f]{32}$')
        arq = os.path.join(self.pasta_anexos, j['id'] + '.png')
        self.assertEqual(oct(os.stat(arq).st_mode & 0o777), '0o600')
        self.assertEqual(oct(os.stat(self.pasta_anexos).st_mode & 0o777), '0o700')

    async def test_tipo_nao_permitido_e_assinatura_falsa(self):
        self.assertEqual((await self.enviar(PNG, 'text/html'))[0], 415)
        self.assertEqual((await self.enviar(PNG, 'application/octet-stream'))[0], 415)
        self.assertEqual((await self.enviar(b'<script>alert(1)</script>' * 4, 'image/png'))[0], 400)
        self.assertEqual((await self.enviar(PNG, 'image/gif'))[0], 400)
        self.assertEqual((await self.enviar(b'', 'image/png'))[0], 400)

    async def test_tamanho_maximo_15mb(self):
        self.assertEqual((await self.enviar(PNG + b'\0' * (15 * 1024 * 1024)))[0], 413)
        self.assertEqual((await self.enviar(PNG + b'\0' * (15 * 1024 * 1024 - len(PNG) - 8)))[0], 200)

    async def test_exige_host_origem_e_token(self):
        self.assertEqual((await self.enviar(PNG, **{'X-Token': 'errado'}))[0], 403)
        self.assertEqual((await self.enviar(PNG, Origin='https://evil.com'))[0], 403)
        self.assertEqual((await self.enviar(PNG, Host='atacante.com:%d' % self.porta))[0], 403)
        self.assertFalse(os.path.exists(self.pasta_anexos) and os.listdir(self.pasta_anexos))


class TestAnexosDisco(unittest.TestCase):
    def test_limpeza_de_mais_de_7_dias(self):
        import time as _t
        g = anexos.Anexos(os.path.join(tempfile.mkdtemp(), 'a'))
        velho, novo = g.salvar('image/png', PNG), g.salvar('image/png', PNG)
        antigo = _t.time() - 8 * 86400
        os.utime(g.caminho(velho), (antigo, antigo))
        self.assertEqual(g.limpar_antigos(), 1)
        self.assertIsNone(g.caminho(velho))
        self.assertIsNotNone(g.caminho(novo))

    def test_id_malformado_nunca_vira_caminho(self):
        g = anexos.Anexos(tempfile.mkdtemp())
        for ruim in ('../../etc/passwd', 'a' * 32 + '/../x', '', None, 5, 'ZZ' * 16):
            self.assertIsNone(g.caminho(ruim))


class TestComposerWS(Base):
    async def conectar(self):
        return await self.sessao.ws_connect('%s/ws/terminal?id=%s&token=%s' % (self.base, JOB, TOKEN), headers={'Origin': self.base})

    async def test_enviar_com_anexo_cola_o_caminho_depois_o_texto_depois_enter(self):
        _, j = await TestAnexo.enviar(self, PNG)
        ws = await self.conectar()
        await ws.send_json({'tipo': 'enviar', 'texto': 'oi\nlinha 2', 'anexos': [j['id']]})
        await asyncio.sleep(0.5)
        await ws.close()
        await asyncio.sleep(0.2)
        esc = self.terminais_falsos[0].escritos
        caminho = os.path.join(self.pasta_anexos, j['id'] + '.png').encode()
        self.assertEqual(esc, [b'\x1b[200~' + caminho + b'\x1b[201~', b'\x1b[200~oi\nlinha 2\x1b[201~', b'\r'])

    async def test_uuid_invalido_ou_inexistente_nao_envia_nada(self):
        ws = await self.conectar()
        for ids in (['x' * 32], ['../../etc/passwd'], ['a' * 32], [5]):
            await ws.send_json({'tipo': 'enviar', 'texto': 'oi', 'anexos': ids})
            msg = await ws.receive(timeout=3)
            self.assertEqual(json.loads(msg.data)['tipo'], 'erro', ids)
        await ws.send_json({'tipo': 'enviar', 'texto': '   ', 'anexos': []})
        self.assertEqual(json.loads((await ws.receive(timeout=3)).data)['tipo'], 'erro')
        await ws.close()
        self.assertEqual(self.terminais_falsos[0].escritos, [])

    async def test_esc_no_texto_nao_escapa_do_paste(self):
        ws = await self.conectar()
        await ws.send_json({'tipo': 'enviar', 'texto': 'a\x1b[201~rm -rf /'})
        await asyncio.sleep(0.3)
        await ws.close()
        self.assertEqual(self.terminais_falsos[0].escritos[0], b'\x1b[200~a[201~rm -rf /\x1b[201~')

    async def test_comandos_so_da_lista_branca(self):
        ws = await self.conectar()
        for nome, valor in (('model', 'opus'), ('effort', 'xhigh'), ('model', 'gpt-9'), ('effort', 'turbo'), ('quit', 'x'),
                            ('model', 'opus; rm -rf /'), ('model', None), ('effort', ['low'])):
            await ws.send_json({'tipo': 'comando', 'nome': nome, 'valor': valor})
        await ws.send_json({'tipo': 'interromper'})
        await asyncio.sleep(0.5)
        await ws.close()
        self.assertEqual(self.terminais_falsos[0].escritos, [b'/model opus', b'\r', b'/effort xhigh', b'\r', b'\x1b'])


class TestWebSocket(Base):
    async def ws(self, query, origem='auto', host=None):
        h = {}
        if origem == 'auto':
            h['Origin'] = self.base
        elif origem:
            h['Origin'] = origem
        if host:
            h['Host'] = host
        try:
            async with self.sessao.ws_connect(self.base + '/ws/terminal?' + query, headers=h) as w:
                return 101, w
        except Exception as e:  # WSServerHandshakeError
            return getattr(e, 'status', None), None

    async def test_recusas_antes_do_upgrade(self):
        casos = [
            ('id=%s&token=%s' % (JOB, 'errado'), 'auto', None, 403),
            ('id=%s' % JOB, 'auto', None, 403),                                  # sem token
            ('id=%s&token=%s' % (JOB, TOKEN), 'https://evil.com', None, 403),    # origem errada
            ('id=%s&token=%s' % (JOB, TOKEN), None, None, 403),                  # sem origem
            ('id=%s&token=%s' % (JOB, TOKEN), 'auto', 'atacante.com:%d' % self.porta, 403),
            ('id=--evil&token=%s' % TOKEN, 'auto', None, 400),
            ('id=zz99yy88&token=%s' % TOKEN, 'auto', None, 404),                 # inexistente
            ('id=%s&token=%s' % (SID_ABERTA, TOKEN), 'auto', None, 409),         # interativa: nao e background
        ]
        for q, origem, host, esperado in casos:
            status, _ = await self.ws(q, origem, host)
            self.assertEqual(status, esperado, q)

    async def test_sessao_interativa_pelo_id_da_sessao_nao_abre_terminal(self):
        # mesmo que alguem passe o id de uma sessao interativa como se fosse job: 409, nunca spawn
        self.estado.estado['sessoes'][0]['jobId'] = 'ab99cd99'
        status, _ = await self.ws('id=ab99cd99&token=%s' % TOKEN)
        self.assertEqual(status, 409)

    async def test_job_sem_registro_listado_pelo_daemon_abre_terminal(self):
        # CLI 2.1.296: --bg sem prompt so grava ~/.claude/sessions depois do primeiro prompt
        self.saida = json.dumps([{'id': 'ee77ff66', 'kind': 'background', 'state': 'blocked'}])
        status, _ = await self.ws('id=ee77ff66&token=%s' % TOKEN)
        self.assertEqual(status, 101)
        self.assertIn((['claude', 'agents', '--json'], self.home), self.chamadas)

    async def test_job_listado_mas_nao_background_nao_abre_terminal(self):
        self.saida = json.dumps([{'id': 'ee77ff66', 'kind': 'interactive'}])
        status, _ = await self.ws('id=ee77ff66&token=%s' % TOKEN)
        self.assertEqual(status, 404)

    async def test_limite_de_terminais(self):
        antigo = servidor.MAX_TERMINAIS
        servidor.MAX_TERMINAIS = 0
        try:
            status, _ = await self.ws('id=%s&token=%s' % (JOB, TOKEN))
        finally:
            servidor.MAX_TERMINAIS = antigo
        self.assertEqual(status, 429)



class TestConversa(Base):
    async def get(self, rota, **cab):
        h = {'X-Token': TOKEN}
        h.update(cab)
        async with self.sessao.get(self.base + rota, headers={k: v for k, v in h.items() if v is not None}) as r:
            return r.status, await r.json(content_type=None)

    async def test_le_o_transcrito_da_sessao_com_token(self):
        with open(os.path.join(self.pasta, SID_BG + '.jsonl'), 'w') as f:
            f.write(json.dumps({'type': 'user', 'uuid': 'u1', 'message': {'role': 'user', 'content': 'oi'}}) + '\n')
        status, r = await self.get('/api/conversa/' + SID_BG)
        self.assertEqual(status, 200)
        self.assertEqual([i['texto'] for i in r['itens']], ['oi'])
        status, r2 = await self.get('/api/conversa/%s?desde=%d' % (SID_BG, r['cursor']))
        self.assertEqual((status, r2['itens']), (200, []))

    async def test_sem_token_id_invalido_offset_invalido_e_inexistente(self):
        self.assertEqual((await self.get('/api/conversa/' + SID_BG, **{'X-Token': None}))[0], 403)
        self.assertEqual((await self.get('/api/conversa/' + SID_BG, **{'X-Token': 'errado'}))[0], 403)
        self.assertEqual((await self.get('/api/conversa/..%2F..%2Fetc'))[0], 400)
        self.assertEqual((await self.get('/api/conversa/%s?desde=-1' % SID_BG))[0], 400)
        self.assertEqual((await self.get('/api/conversa/%s?ate=abc' % SID_BG))[0], 400)
        self.assertEqual((await self.get('/api/conversa/' + SID_FECHADA))[0], 404)


if __name__ == '__main__':
    unittest.main()


class TestMarcadorImagem(unittest.TestCase):
    """Redesenho do prompt repete [Image #1]: so um numero NOVO prova que a proxima imagem entrou."""

    def test_redesenho_nao_conta_como_imagem_nova(self):
        atual = servidor.maior_imagem(b'\x1b[2K> [Image #1] ', 0)
        self.assertEqual(atual, 1)
        # o TUI redesenha a linha com a mesma imagem varias vezes enquanto processa a segunda
        atual = servidor.maior_imagem(b'\x1b[2K> [Image #1] \x1b[2K> [Image #1] Pasting...', atual)
        self.assertEqual(atual, 1)
        atual = servidor.maior_imagem(b'> [Image #1] [Image #2] ', atual)
        self.assertEqual(atual, 2)

    def test_numeracao_que_continua_na_conversa(self):
        self.assertEqual(servidor.maior_imagem(b'[Image #7] [Image #8]', 6), 8)
        self.assertEqual(servidor.maior_imagem(b'sem marcador', 3), 3)
