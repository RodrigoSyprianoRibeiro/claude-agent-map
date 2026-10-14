"""Teste fim a fim do terminal embutido, com `claude` de verdade (gasta alguns tokens).

Nao roda junto dos testes unitarios: E2E=1 E2E_NO=<regiao/no> python3 -m unittest discover -s tests -p test_e2e_terminal.py
E2E_NO e um no do SEU mapa com pasta confiavel (ex.: meu-projeto/geral). Cria a sessao `teste-mapa-e2e`
nele e SEMPRE a remove no final (stop + rm).
"""

import asyncio
import glob
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest

from aiohttp import ClientSession
from aiohttp.test_utils import TestServer, unused_port

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import limpeza_teste  # noqa: E402
import servidor  # noqa: E402

NOME = 'teste-mapa-e2e'
NO_E2E = os.environ.get('E2E_NO', '')


def agentes():
    r = subprocess.run(['claude', 'agents', '--json', '--all'], capture_output=True, text=True, timeout=60)
    return json.loads(r.stdout or '[]')


def da_sessao_de_teste(job):
    return [a for a in agentes() if job in (a.get('jobId'), a.get('id'), a.get('name')) or a.get('name') == NOME]


@unittest.skipUnless(os.environ.get('E2E') == '1' and NO_E2E, 'defina E2E=1 e E2E_NO=<regiao/no> para rodar com claude de verdade')
class TestTerminalE2E(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.porta = unused_port()
        self.token = 'tok-e2e'
        self.estado = servidor.Estado(os.path.expanduser('~/.claude'), 14, os.path.join(tempfile.mkdtemp(), 'posicoes.json'),
                                      servidor.configuracao.carregar())
        self.srv = TestServer(servidor.criar_app(self.estado, self.porta, self.token), host='127.0.0.1', port=self.porta)
        await self.srv.start_server()
        self.http = ClientSession()
        self.base = 'http://127.0.0.1:%d' % self.porta
        self.job = None

    async def asyncTearDown(self):
        await self.http.close()
        await self.srv.close()
        if self.job:  # nunca deixa sessao de teste sobrando (nem o transcrito, que o rm nao apaga)
            limpeza_teste.remover_sessao_de_teste(self.job)

    async def test_ciclo_completo(self):
        h = {'Origin': self.base, 'X-Token': self.token}
        async with self.http.post(self.base + '/api/nova-sessao', headers=h, json={
                'no': NO_E2E, 'prompt': 'Responda apenas: ok', 'nome': NOME}) as r:
            corpo = await r.json()
            self.assertEqual(r.status, 200, corpo)
        self.job = corpo['jobId']
        print('jobId:', self.job, '|', corpo['saida'].strip()[:120])

        saida = b''
        async with self.http.ws_connect('%s/ws/terminal?id=%s&token=%s&cols=120&linhas=32' % (self.base, self.job, self.token),
                                        headers={'Origin': self.base}) as ws:
            async def ler_ate(trecho, limite):
                nonlocal saida
                fim = time.monotonic() + limite
                while trecho not in saida and time.monotonic() < fim:
                    try:
                        msg = await ws.receive(timeout=1)
                    except asyncio.TimeoutError:
                        continue
                    if msg.type == 2:  # BINARY
                        saida += msg.data
                    elif msg.type in (8, 256, 257):  # CLOSE/CLOSED/ERROR
                        break
                return trecho in saida

            await ler_ate(b'\x00', 8)  # deixa a tela de attach desenhar
            await ws.send_str('diga apenas: segundo')
            await asyncio.sleep(0.5)
            await ws.send_str('\r')
            achou = await ler_ate(b'segundo', 150)
            print('saida (fim):', saida[-400:].decode('utf-8', 'replace'))
            self.assertTrue(achou, 'a palavra "segundo" nao apareceu na saida do terminal')
        # a digitacao chegou de verdade: o modelo respondeu (o eco na tela sozinho nao prova isso)
        sessao_id = next(a['sessionId'] for a in agentes() if a.get('id') == self.job)
        respostas = ''
        for _ in range(60):
            for caminho in glob.glob(os.path.expanduser('~/.claude/projects/*/%s.jsonl' % sessao_id)):
                with open(caminho, errors='replace') as arq:
                    for linha in arq:
                        try:
                            j = json.loads(linha)
                        except ValueError:
                            continue
                        if j.get('type') == 'assistant':
                            for b in (j.get('message') or {}).get('content') or []:
                                if isinstance(b, dict) and b.get('type') == 'text':
                                    respostas += b.get('text', '') + '\n'
            if 'segundo' in respostas.lower():
                break
            respostas = ''
            await asyncio.sleep(2)
        self.assertIn('segundo', respostas.lower(), 'o modelo nao respondeu ao que foi digitado no terminal')
        # WS fechado: a sessao --bg tem que continuar viva
        await asyncio.sleep(3)
        viva = da_sessao_de_teste(self.job)
        print('apos fechar o WS:', json.dumps(viva)[:300])
        self.assertTrue(viva, 'a sessao sumiu depois de fechar o terminal')
        ps = subprocess.run(['pgrep', '-af', 'claude attach ' + self.job], capture_output=True, text=True).stdout
        self.assertEqual(ps.strip(), '', 'sobrou processo attach: ' + ps)


@unittest.skipUnless(os.environ.get('E2E') == '1' and NO_E2E, 'defina E2E=1 e E2E_NO=<regiao/no> para rodar com claude de verdade')
class TestComposerE2E(TestTerminalE2E):
    """Composer: imagem anexada + texto pelo WS, comando /model, com haiku/low para gastar pouco."""

    async def test_ciclo_completo(self):  # herdado: ja coberto acima, nao repete
        self.skipTest('coberto em TestTerminalE2E')

    def gerar_png(self):
        import cv2
        import numpy as np
        img = np.full((140, 520, 3), 255, np.uint8)
        cv2.putText(img, 'BANANA 4821', (20, 90), cv2.FONT_HERSHEY_SIMPLEX, 2, (0, 0, 0), 4)
        ok, dados = cv2.imencode('.png', img)
        self.assertTrue(ok)
        return dados.tobytes()

    def respostas_e_imagens(self, sessao_id):
        texto, imagens = '', 0
        for caminho in glob.glob(os.path.expanduser('~/.claude/projects/*/%s.jsonl' % sessao_id)):
            with open(caminho, errors='replace') as arq:
                for linha in arq:
                    try:
                        j = json.loads(linha)
                    except ValueError:
                        continue
                    conteudo = (j.get('message') or {}).get('content')
                    if not isinstance(conteudo, list):
                        continue
                    for b in conteudo:
                        if not isinstance(b, dict):
                            continue
                        if j.get('type') == 'user' and b.get('type') == 'image':
                            imagens += 1
                        if j.get('type') == 'assistant' and b.get('type') == 'text':
                            texto += b.get('text', '') + '\n'
        return texto, imagens

    async def test_composer_imagem_texto_e_comando(self):
        h = {'Origin': self.base, 'X-Token': self.token}
        async with self.http.post(self.base + '/api/nova-sessao', headers=h, json={
                'no': NO_E2E, 'prompt': 'Responda apenas: ok', 'nome': NOME,
                'modelo': 'haiku', 'esforco': 'low'}) as r:
            corpo = await r.json()
            self.assertEqual(r.status, 200, corpo)
        self.job = corpo['jobId']
        async with self.http.post(self.base + '/api/anexo', headers=dict(h, **{'Content-Type': 'image/png'}), data=self.gerar_png()) as r:
            anexo = await r.json()
            self.assertEqual(r.status, 200, anexo)
        self.assertEqual(list(anexo), ['id'])
        caminho_anexo = servidor.anexos.Anexos().caminho(anexo['id'])
        self.assertTrue(caminho_anexo)
        saida = bytearray()
        try:
            async with self.http.ws_connect('%s/ws/terminal?id=%s&token=%s&cols=120&linhas=32' % (self.base, self.job, self.token),
                                            headers={'Origin': self.base}) as ws:
                async def ler(segundos):
                    fim = time.monotonic() + segundos
                    while time.monotonic() < fim:
                        try:
                            msg = await ws.receive(timeout=0.5)
                        except asyncio.TimeoutError:
                            continue
                        if msg.type == 2:
                            saida.extend(msg.data)
                        elif msg.type in (8, 256, 257):
                            return
                await ler(12)  # deixa o "ok" inicial terminar
                self.assertIn(b'Haiku', bytes(saida), 'a sessao nao subiu como Haiku')
                await ws.send_json({'tipo': 'enviar', 'anexos': [anexo['id']],
                                    'texto': 'Qual e o texto escrito na imagem? Responda so com o texto.'})
                sessao_id = next(a['sessionId'] for a in agentes() if a.get('id') == self.job)
                texto, imagens = '', 0
                for _ in range(40):
                    await ler(3)
                    texto, imagens = self.respostas_e_imagens(sessao_id)
                    if '4821' in texto:
                        break
                print('imagens no transcrito:', imagens, '| resposta:', texto.strip()[-120:])
                self.assertGreaterEqual(imagens, 1, 'o transcrito nao tem bloco image')
                self.assertIn('4821', texto, 'o modelo nao leu o texto da imagem')
                # comando /model pela lista branca
                saida.clear()
                await ws.send_json({'tipo': 'comando', 'nome': 'model', 'valor': 'sonnet'})
                await ler(6)
                print('apos /model sonnet:', bytes(saida)[-300:].decode('utf-8', 'replace'))
                self.assertIn(b'Sonnet', bytes(saida), '/model sonnet nao trocou o modelo')
        finally:
            if caminho_anexo and os.path.exists(caminho_anexo):
                os.remove(caminho_anexo)


def servico_instalado():
    r = subprocess.run(['systemctl', '--user', 'is-active', 'mapa-agentes'], capture_output=True, text=True)
    return r.stdout.strip() == 'active'


@unittest.skipUnless(os.environ.get('E2E') == '1' and NO_E2E, 'defina E2E=1 e E2E_NO=<regiao/no> para rodar com claude de verdade')
@unittest.skipUnless(servico_instalado(), 'AVISO: servico mapa-agentes nao esta ativo (rode instalar.sh); regressao do restart pulada')
class TestReinicioDoServicoE2E(unittest.IsolatedAsyncioTestCase):
    """Regressao: reiniciar o servico NAO pode matar o daemon do claude nem as sessoes --bg."""
    BASE = 'http://127.0.0.1:8767'

    async def asyncSetUp(self):
        self.http = ClientSession()
        self.job = None

    async def asyncTearDown(self):
        await self.http.close()
        if self.job:  # nunca deixa sessao de teste sobrando (nem o transcrito, que o rm nao apaga)
            limpeza_teste.remover_sessao_de_teste(self.job)

    async def token(self):
        async with self.http.get(self.BASE + '/') as r:
            html = await r.text()
        return html.split("const TOKEN = '")[1].split("'")[0]

    async def esperar_servico(self):
        for _ in range(40):
            try:
                async with self.http.get(self.BASE + '/api/estado') as r:
                    if r.status == 200:
                        return
            except Exception:
                pass
            await asyncio.sleep(0.5)
        self.fail('o servico nao voltou depois do restart')

    def sessao(self):
        return next((a for a in agentes() if a.get('id') == self.job), None)

    async def test_sessao_e_terminal_sobrevivem_ao_restart(self):
        tok = await self.token()
        h = {'Origin': self.BASE, 'X-Token': tok}
        async with self.http.post(self.BASE + '/api/nova-sessao', headers=h, json={
                'no': NO_E2E, 'prompt': 'Responda apenas: ok', 'nome': 'teste-mapa-restart',
                'modelo': 'haiku', 'esforco': 'low'}) as r:
            corpo = await r.json()
            self.assertEqual(r.status, 200, corpo)
        self.job = corpo['jobId']
        await asyncio.sleep(4)
        antes = self.sessao()
        self.assertTrue(antes and antes.get('pid'), 'sessao nao apareceu em claude agents')
        pid = antes['pid']
        # a sessao e o daemon tem que estar FORA do cgroup do servico
        with open('/proc/%d/cgroup' % pid) as f:
            self.assertNotIn('mapa-agentes.service', f.read(), 'a sessao esta no cgroup do servico')
        status = subprocess.run(['systemctl', '--user', 'status', 'mapa-agentes', '--no-pager'], capture_output=True, text=True).stdout
        procs = [l for l in status.split('CGroup:')[1].split('\n\n')[0].splitlines() if '\u2500' in l]
        self.assertTrue(procs and all('servidor.py' in l or 'claude attach' in l for l in procs), 'cgroup do servico: %s' % procs)

        # terminal aberto durante o restart (o attach nao pode sobrar orfao)
        ws = await self.http.ws_connect('%s/ws/terminal?id=%s&token=%s&cols=120&linhas=32' % (self.BASE, self.job, tok), headers={'Origin': self.BASE})
        await asyncio.sleep(3)
        self.assertTrue(subprocess.run(['pgrep', '-f', 'claude attach ' + self.job], capture_output=True).returncode == 0, 'attach nao subiu')

        subprocess.run(['systemctl', '--user', 'restart', 'mapa-agentes'], check=True, timeout=60)
        await ws.close()
        await self.esperar_servico()
        await asyncio.sleep(2)

        depois = self.sessao()
        self.assertTrue(depois and depois.get('pid') == pid, 'a sessao nao sobreviveu ao restart: %s' % depois)
        self.assertTrue(os.path.exists('/proc/%d' % pid), 'o processo da sessao morreu')
        orfaos = subprocess.run(['pgrep', '-af', 'claude attach ' + self.job], capture_output=True, text=True).stdout
        orfaos = [l for l in orfaos.splitlines() if 'pgrep' not in l and 'bash -c' not in l]
        self.assertEqual(orfaos, [], 'sobrou attach orfao depois do restart: %s' % orfaos)

        # o terminal reabre (token novo) e aceita digitacao
        tok = await self.token()
        saida = bytearray()
        async with self.http.ws_connect('%s/ws/terminal?id=%s&token=%s&cols=120&linhas=32' % (self.BASE, self.job, tok), headers={'Origin': self.BASE}) as ws2:
            await asyncio.sleep(3)
            await ws2.send_json({'tipo': 'enviar', 'texto': 'diga apenas: reaberto'})
            sessao_id = depois['sessionId']
            texto = ''
            fim = time.monotonic() + 90
            while time.monotonic() < fim and 'reaberto' not in texto.lower():
                try:
                    msg = await ws2.receive(timeout=2)
                    if msg.type == 2:
                        saida.extend(msg.data)
                except asyncio.TimeoutError:
                    pass
                texto = ''
                for caminho in glob.glob(os.path.expanduser('~/.claude/projects/*/%s.jsonl' % sessao_id)):
                    with open(caminho, errors='replace') as arq:
                        for linha in arq:
                            try:
                                j = json.loads(linha)
                            except ValueError:
                                continue
                            if j.get('type') == 'assistant':
                                for b in (j.get('message') or {}).get('content') or []:
                                    if isinstance(b, dict) and b.get('type') == 'text':
                                        texto += b.get('text', '') + '\n'
            self.assertIn('reaberto', texto.lower(), 'o terminal reaberto nao aceitou digitacao')


@unittest.skipUnless(os.environ.get('E2E') == '1' and NO_E2E, 'defina E2E=1 e E2E_NO=<regiao/no> para rodar com claude de verdade')
class TestConfiancaE2E(unittest.IsolatedAsyncioTestCase):
    """Pasta nova (repo git sob a home) nao e confiavel: `--bg` falha; o app abre o dialogo oficial, aceita, e o `--bg` passa."""

    async def asyncSetUp(self):
        import secrets
        import confianca
        self.confianca = confianca
        self.pasta = os.path.expanduser('~/dev/.mapa-teste-confianca-%s' % secrets.token_hex(3))
        os.makedirs(self.pasta)
        subprocess.run(['git', 'init', '-q'], cwd=self.pasta, check=True)
        self.projeto = confianca.pasta_do_projeto(self.pasta)
        self.antes = confianca.transcritos(self.projeto)
        self.job = None
        self.porta = unused_port()
        self.token = 'tok-conf'
        estado = servidor.Estado(os.path.expanduser('~/.claude'), 14, os.path.join(tempfile.mkdtemp(), 'posicoes.json'),
                                      servidor.configuracao.carregar())
        original = estado.obter
        pasta = self.pasta

        async def obter():  # no so do teste, com a pasta temporaria
            est = await original()
            return dict(est, nos=est['nos'] + [{'id': 'teste/confianca', 'regiao': 'teste', 'nome': 'confianca', 'caminho': pasta}])

        estado.obter = obter
        self.srv = TestServer(servidor.criar_app(estado, self.porta, self.token), host='127.0.0.1', port=self.porta)
        await self.srv.start_server()
        self.http = ClientSession()
        self.base = 'http://127.0.0.1:%d' % self.porta

    async def asyncTearDown(self):
        import shutil
        await self.http.close()
        await self.srv.close()
        if self.job:
            limpeza_teste.remover_sessao_de_teste(self.job)
        shutil.rmtree(self.pasta, ignore_errors=True)
        # pasta de projeto do Claude para o diretorio de teste: so remove o que esta vazio (nunca rm -rf)
        for sub in ('memory',):
            try:
                os.rmdir(os.path.join(self.projeto, sub))
            except OSError:
                pass
        try:
            os.rmdir(self.projeto)
        except OSError:
            pass

    async def nova_sessao(self):
        h = {'Origin': self.base, 'X-Token': self.token}
        async with self.http.post(self.base + '/api/nova-sessao', headers=h, json={
                'no': 'teste/confianca', 'prompt': 'Responda apenas: ok', 'nome': 'teste-confianca', 'modelo': 'haiku', 'esforco': 'low'}) as r:
            return r.status, await r.json()

    async def dialogo(self, tecla_aceitar):
        """Abre /ws/confiar, espera o dialogo oficial e responde. Devolve o desfecho avisado pelo servidor."""
        saida = bytearray()
        async with self.http.ws_connect('%s/ws/confiar?no=teste/confianca&token=%s&cols=120&linhas=32' % (self.base, self.token),
                                        headers={'Origin': self.base}) as ws:
            fim = time.monotonic() + 30
            while 'Quick safety check' not in self.confianca.texto_limpo(bytes(saida)) and time.monotonic() < fim:
                try:
                    m = await ws.receive(timeout=1)
                except asyncio.TimeoutError:
                    continue
                if m.type == 2:
                    saida.extend(m.data)
            self.assertIn('Quick safety check', self.confianca.texto_limpo(bytes(saida)), 'o dialogo oficial de confianca nao apareceu')
            await asyncio.sleep(1)
            if tecla_aceitar:
                await ws.send_str('\x1b[B')  # "Yes, I trust this folder" (o cursor comeca em "No, exit")
                await asyncio.sleep(0.4)
            await ws.send_str('\r')
            fim = time.monotonic() + 30
            while time.monotonic() < fim:
                try:
                    m = await ws.receive(timeout=1)
                except asyncio.TimeoutError:
                    continue
                if m.type == 1:
                    return json.loads(m.data)
                if m.type in (8, 256, 257):
                    break
        return None

    async def test_confiar_na_pasta(self):
        st, j = await self.nova_sessao()
        self.assertEqual(st, 400, j)
        self.assertEqual(j.get('codigo'), 'nao_confiavel', j)
        self.assertIn('not trusted', j['erro'])
        # recusar ("No, exit" e o padrao do dialogo): o servidor avisa e a pasta continua nao confiavel
        self.assertEqual(await self.dialogo(False), {'tipo': 'confianca', 'estado': 'recusada'})
        st, j = await self.nova_sessao()
        self.assertEqual((st, j.get('codigo')), (400, 'nao_confiavel'))
        # aceitar
        self.assertEqual(await self.dialogo(True), {'tipo': 'confianca', 'estado': 'aceita'})
        await asyncio.sleep(1)
        ps = subprocess.run(['pgrep', '-af', 'claude'], capture_output=True, text=True).stdout
        self.assertNotIn(self.pasta, ps, 'o claude interativo da confianca ficou rodando')
        st, j = await self.nova_sessao()
        self.assertEqual(st, 200, j)
        self.job = j['jobId']
        # nenhum transcrito vazio sobrou do claude interativo (so o da sessao de teste, que o teardown remove)
        novos = self.confianca.transcritos(self.projeto) - self.antes
        self.assertLessEqual(len(novos), 1, novos)


if __name__ == '__main__':
    unittest.main()
