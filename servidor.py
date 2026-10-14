#!/usr/bin/env python3
"""Mapa de agentes: mapa 2D das sessoes do Claude Code da maquina.

Reaproveita o painel-sessoes (mesma leitura de transcritos, sessoes abertas e alertas) e
acrescenta o layout dos nos. Escuta so em 127.0.0.1: os transcritos tem comandos e segredos.
Todo request exige Host local; POST exige tambem Origin local e o token desta execucao,
que so a pagina servida conhece.

Uso: python3 servidor.py [--porta 8767] [--dias 14] [--abrir] [--foco empresa/portal] [--config arquivo.json]
Com o servidor ja rodando, `--abrir --foco X` so abre mais uma janela. Workspaces e regioes:
~/.config/mapa-agentes/config.json (ver config.exemplo.json).
"""

import argparse
import asyncio
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from urllib.parse import quote

from aiohttp import web

AQUI = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, AQUI)
import painel  # noqa: E402
import layout  # noqa: E402
import detalhe  # noqa: E402
import ambiente  # noqa: E402
import config as configuracao  # noqa: E402
import confianca  # noqa: E402
import terminal  # noqa: E402
import anexos  # noqa: E402
import uso  # noqa: E402
import conversa  # noqa: E402

CACHE_SEGUNDOS = 2
MAX_TERMINAIS = 8
FALHA_DO_SYSTEMD_RUN = re.compile(r'Failed to (connect to bus|create bus|start transient|allocate|register|set up)')
MODELOS = ('opus', 'sonnet', 'haiku', 'fable')
ESFORCOS = ('low', 'medium', 'high', 'xhigh', 'max')
MAX_TEXTO_ENVIO = 100_000
ESPERA_IMAGEM_MAX = 6.0   # segundos esperando o CLI terminar de anexar ("Pasting...")
MARCADOR_IMAGEM = re.compile(rb'\[Image #(\d+)')


def maior_imagem(saida, atual):
    """Maior N de "[Image #N" em `saida`, ou `atual` se nenhum for maior."""
    return max([atual] + [int(n) for n in MARCADOR_IMAGEM.findall(saida)])
PAUSA_APOS_IMAGEM = 0.4
PAUSA_ANTES_ENTER = 0.4
COLA_INI, COLA_FIM = b'\x1b[200~', b'\x1b[201~'
ID_JOB = re.compile(r'^[0-9A-Za-z][0-9A-Za-z_-]{3,63}$')
ID_SESSAO = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
JOB_NA_SAIDA = re.compile(r'backgrounded\s*[·\-:]\s*([0-9A-Za-z][0-9A-Za-z_-]{3,63})')
MSG_ASSUMIR_ABERTA = 'feche a aba no VSCode antes de assumir aqui'


def extrair_job_id(saida):
    """Id impresso por `claude --bg` ("backgrounded · <id> · ..."), ou None."""
    m = JOB_NA_SAIDA.search(saida or '')
    return m.group(1) if m else None


def descobrir_job(executar, pasta, antes):
    """Plano B quando a saida do --bg nao traz o id: sessao background nova na pasta (`claude agents --json`)."""
    try:
        lista = json.loads(executar(['claude', 'agents', '--json'], pasta))
    except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired):
        return None
    novas = [x for x in lista if isinstance(x, dict) and x.get('kind') == 'background'
             and x.get('cwd') == pasta and (x.get('jobId') or x.get('id')) not in antes]
    novas.sort(key=lambda x: x.get('startedAt') or 0)
    return (novas[-1].get('jobId') or novas[-1].get('id')) if novas else None


def job_listado_pelo_daemon(executar, job, home):
    """Sessao --bg que o daemon lista mas que ainda nao gravou ~/.claude/sessions/<pid>.json.

    Desde o CLI 2.1.296, `claude --bg` sem prompt fica "send a prompt to start": o registro (com jobId)
    so aparece depois do primeiro prompt, mas o attach ja funciona, e e por ele que o prompt chega."""
    try:
        lista = json.loads(executar(['claude', 'agents', '--json'], home))
    except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired):
        return False
    return isinstance(lista, list) and any(
        isinstance(x, dict) and x.get('kind') == 'background' and job in (x.get('jobId'), x.get('id')) for x in lista)


def e_background(sessao):
    """O registro em ~/.claude/sessions grava kind 'bg'; `claude agents --json` diz 'background'."""
    return sessao.get('tipo') in ('bg', 'background')


def comparar(a, b):
    return secrets.compare_digest(str(a).encode(), str(b).encode())
POSICOES = os.path.join(os.path.dirname(configuracao.ARQUIVO), 'posicoes.json')


class Estado:
    """Estado + layout com cache curto, calculado fora do event loop."""

    def __init__(self, base, dias, caminho_posicoes, config=None):
        self.painel = painel.Painel(base, dias)
        self.layout = layout.Layout(caminho_posicoes, config=config)
        self.detalhes = detalhe.Detalhes(base)
        self.cache = None
        self.cache_em = 0
        self.trava = asyncio.Lock()

    def _calcular(self):
        estado = self.painel.estado()
        estado.update(self.layout.montar(estado['sessoes'], estado['geradoEm']))
        return estado

    async def obter(self):
        async with self.trava:
            if self.cache is None or time.monotonic() - self.cache_em > CACHE_SEGUNDOS:
                self.cache = await asyncio.get_running_loop().run_in_executor(None, self._calcular)
                self.cache_em = time.monotonic()
            return self.cache

    def invalidar(self):
        self.cache = None


def argv_em_scope(argv):
    """Roda o claude num scope proprio do systemd --user, FORA do cgroup do servico.

    O primeiro `claude --bg` inicia o "background service" do Claude, que herda o cgroup de quem o
    chamou; reiniciar o servico (KillMode padrao) matava o daemon e todas as sessoes --bg junto.
    Num scope proprio, o daemon sobrevive a restart, stop e desinstalacao deste app."""
    return ['systemd-run', '--user', '--scope', '--quiet', '--collect', '--'] + list(argv)


def iniciar_claude(argv, pasta):
    """Roda um comando `claude` (fora do cgroup do servico) com o ambiente atual do systemd --user."""
    env = ambiente.padrao.obter()

    def rodar(comando):
        return subprocess.run(comando, cwd=pasta, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60, env=env)

    r = None
    if shutil.which('systemd-run'):
        try:
            r = rodar(argv_em_scope(argv))
        except FileNotFoundError:
            r = None
        # falha do PROPRIO systemd-run (sem barramento, sem scope): usa a execucao direta como reserva.
        # So esses erros, para nunca rodar o claude duas vezes (um --bg duplicaria a sessao).
        if r is not None and r.returncode != 0 and FALHA_DO_SYSTEMD_RUN.match(r.stderr.lstrip()):
            r = None
    if r is None:
        r = rodar(list(argv))
    saida = (r.stdout + r.stderr).strip()
    if r.returncode != 0:
        raise RuntimeError(saida or 'claude saiu com codigo %d' % r.returncode)
    return saida


def fabrica_terminal_confianca(pasta, cols, linhas):
    """`claude` interativo (argv fixo) na pasta do no, fora do cgroup do servico, como o resto dos claude."""
    argv = ['claude']
    if shutil.which('systemd-run'):
        argv = argv_em_scope(argv)
    return terminal.Terminal(None, cols, linhas, argv=argv, cwd=pasta)


def nao_confiavel(mensagem):
    return 'not trusted' in str(mensagem).lower()


def criar_app(estado, porta, token, executar=iniciar_claude, home=None, fabrica_terminal=None, pasta_anexos=None, arquivo_uso=None,
              fabrica_confianca=None):
    """executar(argv, pasta) -> saida; levanta RuntimeError se o comando falhar (injetavel nos testes)."""
    home = home or os.path.expanduser('~')
    terminais = set()
    config_job = {}  # jobId -> {'modelo', 'esforco'}: da criacao e do ultimo comando enviado (so em memoria)
    fabrica_terminal = fabrica_terminal or terminal.Terminal
    fabrica_confianca = fabrica_confianca or fabrica_terminal_confianca
    guarda = anexos.Anexos(pasta_anexos)

    @web.middleware
    async def protecao(request, handler):
        if not painel.host_permitido(request.headers.get('Host', ''), porta):
            return web.Response(status=403, text='host nao permitido')
        if request.method == 'POST':
            if (not painel.origem_permitida(request.headers.get('Origin'), porta)
                    or not comparar(request.headers.get('X-Token', ''), token)):
                return web.Response(status=403, text='proibido')
        return await handler(request)

    async def pagina(request):
        def ler():
            with open(os.path.join(AQUI, 'mapa.html'), 'rb') as arq:
                return arq.read().replace(b'__TOKEN__', token.encode())
        html = await asyncio.get_running_loop().run_in_executor(None, ler)
        return web.Response(body=html, content_type='text/html', charset='utf-8', headers={'Cache-Control': 'no-store'})

    async def api_estado(request):
        return web.json_response(await estado.obter(), headers={'Cache-Control': 'no-store'})

    async def api_uso(request):
        dados = await asyncio.get_running_loop().run_in_executor(None, uso.ler, arquivo_uso or uso.ARQUIVO)
        return web.json_response(uso.calcular(dados), headers={'Cache-Control': 'no-store'})

    async def api_sessao(request):
        sid = request.match_info['id']
        r = await asyncio.get_running_loop().run_in_executor(None, estado.detalhes.sessao, sid)
        if r is None:
            return web.json_response({'erro': 'sessao nao encontrada'}, status=404)
        # modelo e esforco escolhidos na criacao ou no ultimo comando: a pilula do composer comeca certa
        job = next((s.get('jobId') for s in (await estado.obter())['sessoes'] if s['id'] == sid), None)
        r['config'] = dict(config_job.get(job, {})) if job else {}
        return web.json_response(r, headers={'Cache-Control': 'no-store'})

    async def api_conversa(request):
        """Conversa da sessao para a vista de chat. Conteudo bruto do transcrito: exige o token, alem do Host."""
        if not comparar(request.headers.get('X-Token', ''), token):
            return web.json_response({'erro': 'proibido'}, status=403)
        sid = request.match_info['id']
        if not ID_SESSAO.match(sid):
            return web.json_response({'erro': 'id invalido'}, status=400)
        try:
            desde, ate = (int(request.query[k]) if request.query.get(k) else None for k in ('desde', 'ate'))
        except ValueError:
            return web.json_response({'erro': 'offset invalido'}, status=400)
        if (desde is not None and desde < 0) or (ate is not None and ate < 0):
            return web.json_response({'erro': 'offset invalido'}, status=400)
        loop = asyncio.get_running_loop()
        caminho = await loop.run_in_executor(None, estado.detalhes.localizar, sid)  # nunca caminho vindo do cliente
        if caminho is None:
            return web.json_response({'erro': 'sessao nao encontrada'}, status=404)
        r = await loop.run_in_executor(None, conversa.ler, caminho, desde, ate)
        return web.json_response(r, headers={'Cache-Control': 'no-store'})

    async def api_posicao(request):
        try:
            dados = await request.json()
            chave, x, y = dados['no'], dados['x'], dados['y']
        except (ValueError, KeyError, TypeError):
            return web.json_response({'erro': 'corpo invalido'}, status=400)
        if not isinstance(chave, str) or not estado.layout.definir_posicao(chave, x, y):
            return web.json_response({'erro': 'posicao ou no invalido'}, status=400)
        estado.invalidar()
        return web.json_response({'ok': True})

    def erro(status, texto, **extra):
        return web.json_response(dict({'erro': texto}, **extra), status=status)

    async def corpo_json(request):
        try:
            dados = await request.json()
        except ValueError:
            return None
        return dados if isinstance(dados, dict) else None

    async def rodar(argv, pasta):
        return await asyncio.get_running_loop().run_in_executor(None, executar, argv, pasta)

    async def achar_sessao(predicado, tentativas=1):
        for i in range(tentativas):
            if i:
                await asyncio.sleep(0.5)
                estado.invalidar()
            for s in (await estado.obter())['sessoes']:
                if predicado(s):
                    return s
        return None

    async def api_nova_sessao(request):
        dados = await corpo_json(request)
        if dados is None:
            return erro(400, 'corpo invalido')
        no = dados.get('no')
        est = await estado.obter()
        alvo = next((n for n in est['nos'] if n['id'] == no), None) if isinstance(no, str) else None
        if alvo is None and isinstance(no, str) and no and '/' not in no:
            # id de REGIAO inteira (foco sem projeto escolhido): usa o geral dela ou o primeiro no com pasta
            da_regiao = sorted((n for n in est['nos'] if n.get('regiao') == no and n.get('caminho')), key=lambda n: (n['id'] != no + '/geral', n['id']))
            alvo = da_regiao[0] if da_regiao else None
        if alvo is None or not alvo.get('caminho'):
            return erro(400, 'no desconhecido ou sem pasta')
        prompt = str(dados.get('prompt') or '').strip()
        try:
            # o cliente nunca manda caminho: a pasta vem do layout. Sem prompt, valida com um marcador e o descarta.
            argv, pasta = painel.validar_nova_sessao(
                {'cwd': alvo['caminho'], 'prompt': prompt or 'x', 'nome': dados.get('nome')}, home)
            if not prompt:
                argv = argv[:-2]
            modelo, esforco = dados.get('modelo') or None, dados.get('esforco') or None
            if modelo not in (None, *MODELOS) or esforco not in (None, *ESFORCOS):
                return erro(400, 'modelo ou esforco fora da lista permitida')
            opcoes = (['--model', modelo] if modelo else []) + (['--effort', esforco] if esforco else [])
            argv = argv[:2] + opcoes + argv[2:]  # antes de --name e do `--`
            antes = {s.get('jobId') for s in est['sessoes'] if s.get('jobId')}
            saida = await rodar(argv, pasta)
        except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as e:
            if nao_confiavel(e):  # o cliente oferece "Confiar nesta pasta" (fluxo oficial na PTY)
                return erro(400, str(e), codigo='nao_confiavel', no=alvo['id'])
            return erro(400, str(e))
        job = extrair_job_id(saida) or await asyncio.get_running_loop().run_in_executor(None, descobrir_job, executar, pasta, antes)
        estado.invalidar()
        if job and (modelo or esforco):
            config_job[job] = {k: x for k, x in (('modelo', modelo), ('esforco', esforco)) if x}
        if not job:
            return erro(502, 'sessao criada, mas o id nao veio na saida: ' + saida[:200])
        return web.json_response({'jobId': job, 'saida': saida})

    async def api_assumir(request):
        dados = await corpo_json(request)
        sid = (dados or {}).get('id')
        if not isinstance(sid, str) or not ID_SESSAO.match(sid):
            return erro(400, 'id invalido')
        s = await achar_sessao(lambda x: x['id'] == sid)
        if s is None:
            return erro(404, 'sessao nao encontrada')
        if s['aberta']:
            return erro(409, MSG_ASSUMIR_ABERTA if not e_background(s) else 'ja esta rodando em segundo plano: abra o terminal')
        try:
            _, pasta = painel.validar_nova_sessao({'cwd': s.get('raiz') or s.get('cwd'), 'prompt': 'x'}, home)
            antes = {x.get('jobId') for x in (await estado.obter())['sessoes'] if x.get('jobId')}
            saida = await rodar(['claude', '--bg', '--resume', sid], pasta)
        except (ValueError, RuntimeError, OSError, subprocess.TimeoutExpired) as e:
            return erro(400, str(e), codigo='nao_confiavel') if nao_confiavel(e) else erro(400, str(e))
        job = extrair_job_id(saida) or await asyncio.get_running_loop().run_in_executor(None, descobrir_job, executar, pasta, antes)
        estado.invalidar()
        if not job:
            return erro(502, 'sessao retomada, mas o id nao veio na saida: ' + saida[:200])
        return web.json_response({'jobId': job, 'saida': saida})

    async def api_parar(request):
        dados = await corpo_json(request)
        job = (dados or {}).get('id')
        if not isinstance(job, str) or not ID_JOB.match(job):
            return erro(400, 'id invalido')
        s = await achar_sessao(lambda x: x.get('jobId') == job and x['aberta'] and e_background(x))
        if s is None:
            return erro(404, 'sessao background nao encontrada')
        try:
            saida = await rodar(['claude', 'stop', job], home)
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
            return erro(400, str(e))
        estado.invalidar()
        return web.json_response({'ok': True, 'saida': saida})

    async def api_anexo(request):
        tipo = (request.headers.get('Content-Type') or '').split(';')[0].strip().lower()
        if tipo not in anexos.TIPOS:
            return erro(415, 'Content-Type deve ser image/png, image/jpeg, image/webp ou image/gif')
        try:
            declarado = int(request.headers.get('Content-Length') or 0)
        except ValueError:
            declarado = 0
        if declarado > anexos.MAX_BYTES:
            return erro(413, 'arquivo maior que 15 MB')
        # leitura em blocos: o client_max_size global (64 KB) so vale para request.read()
        partes, total = [], 0
        async for bloco in request.content.iter_chunked(1 << 16):
            total += len(bloco)
            if total > anexos.MAX_BYTES:
                return erro(413, 'arquivo maior que 15 MB')
            partes.append(bloco)
        try:
            anexo_id = guarda.salvar(tipo, b''.join(partes))
        except ValueError as e:
            return erro(400, str(e))
        return web.json_response({'id': anexo_id})

    async def tratar_mensagem(j, term, ws, vistas, job):
        """Mensagens do composer. O cliente manda uuid e texto; caminhos e sequencias de teclas sao do servidor."""
        tipo = j.get('tipo')
        if tipo == 'interromper':
            term.escrever(b'\x1b')
        elif tipo == 'comando':
            nome, valor = j.get('nome'), j.get('valor')
            permitido = {'model': MODELOS, 'effort': ESFORCOS}.get(nome)
            if permitido is None or valor not in permitido:
                return  # fora da lista branca: ignorado
            term.escrever(('/%s %s' % (nome, valor)).encode())
            config_job.setdefault(job, {})['modelo' if nome == 'model' else 'esforco'] = valor
            await asyncio.sleep(PAUSA_ANTES_ENTER)
            term.escrever(b'\r')
        elif tipo == 'enviar':
            texto = j.get('texto') if isinstance(j.get('texto'), str) else ''
            ids = j.get('anexos') if isinstance(j.get('anexos'), list) else []
            caminhos = [guarda.caminho(i) for i in ids[:10]]
            if len(ids) > 10 or any(c is None for c in caminhos) or len(texto) > MAX_TEXTO_ENVIO or (not texto.strip() and not caminhos):
                await ws.send_str(json.dumps({'tipo': 'erro', 'mensagem': 'anexo desconhecido, texto vazio ou grande demais: nada foi enviado'}))
                return
            for c in caminhos:
                antes = vistas['imagens']
                term.escrever(COLA_INI + c.encode() + COLA_FIM)
                limite = time.monotonic() + ESPERA_IMAGEM_MAX
                while vistas['imagens'] <= antes and time.monotonic() < limite:  # espera o CLI anexar
                    await asyncio.sleep(0.1)
                await asyncio.sleep(PAUSA_APOS_IMAGEM)
            # o texto vai em bracketed paste: quebras de linha nao enviam antes da hora, e ESC nao escapa do paste
            limpo = texto.replace('\x1b', '').replace('\x00', '')
            if limpo:
                term.escrever(COLA_INI + limpo.encode() + COLA_FIM)
                await asyncio.sleep(PAUSA_ANTES_ENTER)
            term.escrever(b'\r')

    async def ws_confiar(request):
        """Terminal de confianca: `claude` interativo na pasta do NO (resolvida aqui, nunca pelo cliente)."""
        origem = request.headers.get('Origin')
        if not origem or not painel.origem_permitida(origem, porta) or not comparar(request.query.get('token', ''), token):
            return web.Response(status=403, text='proibido')
        no = request.query.get('no', '')
        est = await estado.obter()
        alvo = next((n for n in est['nos'] if n['id'] == no), None) if no else None
        if alvo is None or not alvo.get('caminho'):
            return erro(404, 'no desconhecido ou sem pasta')
        try:
            _, pasta = painel.validar_nova_sessao({'cwd': alvo['caminho'], 'prompt': 'x'}, home)  # existe e esta sob a home
        except ValueError as e:
            return erro(400, str(e))
        if len(terminais) >= MAX_TERMINAIS:
            return erro(429, 'limite de %d terminais' % MAX_TERMINAIS)
        projeto = confianca.pasta_do_projeto(pasta)
        antes = confianca.transcritos(projeto)
        try:
            term = fabrica_confianca(pasta, request.query.get('cols'), request.query.get('linhas'))
        except OSError as e:
            return erro(500, 'nao foi possivel abrir o terminal: %s' % e)
        terminais.add(term)
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1 << 20)
        await ws.prepare(request)
        loop = asyncio.get_running_loop()
        fila = asyncio.Queue()
        detector = confianca.Detector()

        def pronto():
            try:
                dados = os.read(term.mestre, 65536)
            except OSError:
                dados = b''
            if dados:
                fila.put_nowait(dados)
            else:  # claude terminou (recusou a confianca, ou foi encerrado por nos)
                loop.remove_reader(term.mestre)
                fila.put_nowait(None)

        async def enviar():
            desfecho = None
            while True:
                dados = await fila.get()
                if dados is None:
                    if desfecho is None:
                        await ws.send_str(json.dumps({'tipo': 'confianca', 'estado': 'recusada'}))
                    await ws.close()
                    return
                await ws.send_bytes(dados)
                if detector.saida(dados) == 'aceita' and desfecho is None:
                    desfecho = 'aceita'
                    # confianca aceita: encerra ESTE claude interativo (SIGHUP no grupo), limpa o transcrito vazio
                    # e avisa o cliente, que refaz o `claude --bg`
                    loop.remove_reader(term.mestre)
                    await loop.run_in_executor(None, term.desanexar)
                    confianca.limpar_transcritos_vazios(projeto, antes)
                    await ws.send_str(json.dumps({'tipo': 'confianca', 'estado': 'aceita'}))
                    await ws.close()
                    return

        loop.add_reader(term.mestre, pronto)
        tarefa = asyncio.create_task(enviar())
        try:
            async for msg in ws:
                dados = msg.data if msg.type == web.WSMsgType.BINARY else (msg.data.encode() if msg.type == web.WSMsgType.TEXT else None)
                if dados is None:
                    continue
                if msg.type == web.WSMsgType.TEXT and msg.data.startswith('{'):
                    try:
                        j = json.loads(msg.data)
                    except ValueError:
                        j = None
                    if isinstance(j, dict) and j.get('tipo') == 'resize':
                        term.redimensionar(j.get('cols'), j.get('linhas'))
                        continue
                detector.entrada(dados)
                term.escrever(dados)
        finally:
            if term.mestre >= 0:
                loop.remove_reader(term.mestre)
            tarefa.cancel()
            await loop.run_in_executor(None, term.desanexar)
            confianca.limpar_transcritos_vazios(projeto, antes)
            terminais.discard(term)
        return ws

    async def ws_terminal(request):
        # execucao de codigo: Host (middleware), Origin e token tem que bater ANTES de aceitar o upgrade
        origem = request.headers.get('Origin')
        if not origem or not painel.origem_permitida(origem, porta) or not comparar(request.query.get('token', ''), token):
            return web.Response(status=403, text='proibido')
        job = request.query.get('id', '')
        if not ID_JOB.match(job):
            return erro(400, 'id invalido')
        if len(terminais) >= MAX_TERMINAIS:
            return erro(429, 'limite de %d terminais' % MAX_TERMINAIS)
        def do_job(x):
            return x['aberta'] and (x.get('jobId') == job or x['id'] == job)
        s = await achar_sessao(do_job)
        if s is None and not await asyncio.get_running_loop().run_in_executor(None, job_listado_pelo_daemon, executar, job, home):
            s = await achar_sessao(do_job, tentativas=6)  # recem-criada: o registro pode demorar a aparecer
            if s is None:
                return erro(404, 'sessao nao encontrada')
        if s is not None and (not e_background(s) or s.get('jobId') != job):
            return erro(409, 'so sessoes background (claude --bg) abrem terminal aqui')
        try:
            term = fabrica_terminal(job, request.query.get('cols'), request.query.get('linhas'))
        except OSError as e:
            return erro(500, 'nao foi possivel abrir o terminal: %s' % e)
        terminais.add(term)
        ws = web.WebSocketResponse(heartbeat=20, max_msg_size=1 << 20)
        await ws.prepare(request)
        loop = asyncio.get_running_loop()
        fila = asyncio.Queue()

        # maior N de "[Image #N]" ja visto na saida: o TUI redesenha a linha do prompt e repete os marcadores
        # antigos, entao contar ocorrencias da falso positivo a partir da segunda imagem; o numero novo nao
        vistas = {'imagens': 0, 'resto': b''}

        def pronto():
            try:
                dados = os.read(term.mestre, 65536)
            except OSError:
                dados = b''
            if dados:
                janela = vistas['resto'] + dados
                vistas['imagens'] = maior_imagem(janela, vistas['imagens'])
                vistas['resto'] = janela[-24:]
                fila.put_nowait(dados)
            else:  # attach terminou
                loop.remove_reader(term.mestre)
                fila.put_nowait(None)

        async def enviar():
            while True:
                dados = await fila.get()
                if dados is None:
                    await ws.close()
                    return
                await ws.send_bytes(dados)

        loop.add_reader(term.mestre, pronto)
        tarefa = asyncio.create_task(enviar())
        try:
            async for msg in ws:
                if msg.type == web.WSMsgType.BINARY:
                    term.escrever(msg.data)
                elif msg.type == web.WSMsgType.TEXT:
                    try:
                        j = json.loads(msg.data) if msg.data.startswith('{') else None
                    except ValueError:
                        j = None
                    if isinstance(j, dict) and j.get('tipo') == 'resize':
                        term.redimensionar(j.get('cols'), j.get('linhas'))
                    elif isinstance(j, dict) and j.get('tipo') in ('enviar', 'comando', 'interromper'):
                        await tratar_mensagem(j, term, ws, vistas, job)
                    else:
                        term.escrever(msg.data.encode())
        finally:
            if term.mestre >= 0:
                loop.remove_reader(term.mestre)
            tarefa.cancel()
            await loop.run_in_executor(None, term.desanexar)
            terminais.discard(term)
        return ws


    app = web.Application(middlewares=[protecao], client_max_size=65536)
    app.router.add_get('/', pagina)
    async def favicon(request):
        return web.FileResponse(os.path.join(AQUI, 'static', 'icones', '32.png'))

    app.router.add_get('/favicon.ico', favicon)
    app.router.add_get('/api/estado', api_estado)
    app.router.add_get('/api/sessao/{id}', api_sessao)
    app.router.add_get('/api/uso', api_uso)
    app.router.add_get('/api/conversa/{id}', api_conversa)
    app.router.add_post('/api/posicao', api_posicao)
    app.router.add_post('/api/anexo', api_anexo)
    app.router.add_post('/api/nova-sessao', api_nova_sessao)
    app.router.add_post('/api/assumir', api_assumir)
    app.router.add_post('/api/parar', api_parar)
    app.router.add_get('/ws/terminal', ws_terminal)
    app.router.add_get('/ws/confiar', ws_confiar)
    app.router.add_static('/static/', os.path.join(AQUI, 'static'), follow_symlinks=False)
    return app


def url_da_pagina(porta, foco):
    return 'http://127.0.0.1:%d/' % porta + ('?foco=' + quote(foco, safe='/-_.') if foco else '')


PERFIL_CHROME = os.path.join(os.path.dirname(configuracao.ARQUIVO), 'chrome')


NAVEGADORES = ('google-chrome', 'google-chrome-stable', 'chromium', 'chromium-browser', 'brave-browser', 'microsoft-edge')


def argv_do_chrome(url, navegador='google-chrome'):
    """Janela de app de verdade: perfil proprio + classe de janela propria (icone e agrupamento
    separados do navegador normal na barra de tarefas e no alt-tab; casa com StartupWMClass da .desktop)."""
    return [navegador, '--app=' + url, '--class=mapa-agentes', '--user-data-dir=' + PERFIL_CHROME]


def abrir_chrome(url):
    """Chrome/Chromium em modo app; sem nenhum deles, o navegador padrao (aba comum)."""
    navegador = next((n for n in NAVEGADORES if shutil.which(n)), None)
    if navegador:
        os.makedirs(PERFIL_CHROME, exist_ok=True)
        argv = argv_do_chrome(url, navegador)
    else:
        argv = ['xdg-open', url]
    subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def porta_ocupada(porta):
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(('127.0.0.1', porta)) == 0


def app_ja_rodando(porta):
    """A porta e do proprio mapa? GET /api/estado local com Host correto devolve o JSON com `regioes`."""
    try:
        req = urllib.request.Request('http://127.0.0.1:%d/api/estado' % porta, headers={'Host': '127.0.0.1:%d' % porta})
        with urllib.request.urlopen(req, timeout=10) as r:
            return 'regioes' in json.load(r)
    except (OSError, ValueError):
        return False


def main():
    parser = argparse.ArgumentParser(description='Agent Map: live map of your Claude Code sessions')
    parser.add_argument('--porta', type=int, default=8767)
    parser.add_argument('--dias', type=float, default=14, help='days of closed sessions to show')
    parser.add_argument('--abrir', action='store_true', help='open the browser window (Chrome/Chromium app mode)')
    parser.add_argument('--foco', default='', help='focus: a workspace, a project or region/project')
    parser.add_argument('--config', default=configuracao.ARQUIVO, help='config.json with workspaces (optional)')
    args = parser.parse_args()
    try:
        cfg = configuracao.carregar(args.config)
    except ValueError as erro:
        parser.error('config: %s' % erro)
    try:
        layout.parse_foco(args.foco)
    except ValueError as erro:
        parser.error(str(erro))
    url = url_da_pagina(args.porta, args.foco.strip())

    if porta_ocupada(args.porta):
        if args.abrir and app_ja_rodando(args.porta):  # so mais uma janela, sem segundo servidor
            abrir_chrome(url)
            print('Agent Map already running; window opened at ' + url, flush=True)
            return
        sys.exit('port %d is in use%s' % (args.porta, '' if not args.abrir else ' by another program'))

    token = secrets.token_urlsafe(24)
    painel.REMOTOS[:] = cfg.remotos
    estado = Estado(os.path.expanduser('~/.claude'), args.dias, POSICOES, cfg)
    app = criar_app(estado, args.porta, token)

    async def ao_subir(_app):
        if args.abrir:  # depois do bind: o on_startup roda antes de o socket escutar
            asyncio.get_running_loop().call_later(1.0, abrir_chrome, url)

    app.on_startup.append(ao_subir)
    removidos = anexos.Anexos().limpar_antigos()
    if removidos:
        print('old attachments removed: %d' % removidos, flush=True)
    print('Agent Map on http://127.0.0.1:%d' % args.porta, flush=True)
    web.run_app(app, host='127.0.0.1', port=args.porta, print=None)


if __name__ == '__main__':
    main()
