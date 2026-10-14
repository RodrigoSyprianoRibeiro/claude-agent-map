#!/usr/bin/env python3
"""Painel local das sessoes do Claude Code da maquina, de todos os projetos.

Junta tres fontes, todas locais:
- ~/.claude/sessions/<pid>.json: registro de cada sessao aberta (cwd, nome, busy/idle);
- ~/.claude/projects/<pasta>/<sessao>.jsonl: transcrito (titulo, prompts, cwd atual, PR);
- ~/.claude/state/atencao/<sessao>.json: gravado pelo hook Notification
  (hooks/notificacao-sessao.sh) quando a sessao pede permissao ou espera voce.

Separa quem precisa de voce, quem esta trabalhando, quem terminou e espera sua vez e as
fechadas recentes. Mostra o subprojeto de cada aba: abrindo o VS Code na raiz de um workspace,
todas as abas tem o mesmo cwd, entao o subprojeto sai do cwd atual e dos arquivos tocados.

Tambem valida a criacao de sessao nova em background (`claude --bg`): a pasta tem que estar
dentro da home (e o proprio claude exige que ela ja seja confiavel).

So biblioteca padrao. Escuta apenas em 127.0.0.1: os transcritos tem comandos, hosts e
segredos, nada disso pode sair da maquina.
"""

import collections
import glob
import json
import os
import re
import threading
import time
from datetime import datetime

# Transcrito chega a 400 MB: na primeira leitura so o final interessa (titulo, ultimo prompt
# e cwd se repetem centenas de vezes). Depois a leitura e incremental.
LEITURA_ABERTA = 1_500_000
LEITURA_FECHADA = 300_000
LEITURA_CABECA = 64_000
# Se a cauda nao tem prompt real do usuario (so notificacoes e resultados de ferramenta), procura para tras
BLOCO_BUSCA_PROMPT = 4_000_000
MAX_BUSCA_PROMPT = 32_000_000
# O alerta do hook vale ate o transcrito ser escrito depois dele (tool aprovada, prompt novo)
FOLGA_ATENCAO = 3
SEGUNDOS_SUBAGENTE = 90
RESPOSTA_NOVA = 30 * 60
RELISTAR_ARQUIVOS = 30
MAX_FECHADAS = 200
MAX_TEXTO = 400
MAX_PROMPT = 20_000


def resumir(texto, limite=MAX_TEXTO):
    texto = ' '.join(str(texto or '').split())
    return texto if len(texto) <= limite else texto[:limite - 1] + '…'


def epoch(carimbo):
    """ISO 8601 do transcrito (2026-10-08T10:54:42.167Z) em epoch segundos, ou None."""
    if not isinstance(carimbo, str):
        return None
    try:
        return datetime.fromisoformat(carimbo.replace('Z', '+00:00')).timestamp()
    except ValueError:
        return None


def processo_vivo(pid, proc_start):
    """Confere o PID e o instante de inicio (campo 22 do stat): PID reaproveitado nao conta."""
    try:
        with open('/proc/%d/stat' % int(pid)) as arq:
            campos = arq.read().rsplit(')', 1)[1].split()
    except (OSError, ValueError, IndexError):
        return False
    return proc_start is None or (len(campos) > 19 and campos[19] == str(proc_start))


def segmento(caminho, raiz, e_arquivo=False):
    """Primeira pasta de `caminho` abaixo de `raiz` (o subprojeto), ou None."""
    if not caminho or not raiz:
        return None
    rel = os.path.relpath(caminho, raiz)
    if rel == '.' or rel.startswith('..'):
        return None
    partes = rel.split(os.sep)
    if e_arquivo and len(partes) < 2:  # arquivo solto na raiz nao diz o subprojeto
        return None
    if partes[0].startswith('.'):
        return None
    return partes[0]


CAMINHO_ABSOLUTO = re.compile(r'''(?:^|[\s'"=])(/[^\s'";|&<>()]+)''')
CD_OU_GIT_C = re.compile(r'''(?:\bcd|\bgit\s+-C)\s+['"]?([^\s'";|&]+)''')


def pastas_do_comando(comando, cwd):
    """Caminhos citados num comando Bash (`cd x &&`, `git -C x`, absolutos), para achar o subprojeto
    de sessoes que trabalham so pelo terminal."""
    if not comando:
        return []
    pastas = CAMINHO_ABSOLUTO.findall(comando)
    for alvo in CD_OU_GIT_C.findall(comando):
        if not alvo.startswith(('/', '~', '$', '-')) and cwd:
            pastas.append(os.path.normpath(os.path.join(cwd, alvo)))
    return pastas[:10]


# Trabalho direto em servidores remotos (opcional, por workspace no config.json): o host do ssh e o
# caminho /var/www/... dizem o repo local. Hosts e caminhos vem de um inventario JSON do workspace e
# `repoDoCaminho` traduz a chave de cada caminho para a pasta do repo.
REMOTOS = []  # [{'pasta', 'inventario', 'repoDoCaminho'}]: preenchido pelo servidor a partir do config
COMANDO_REMOTO = re.compile(r'\b(?:ssh|scp|rsync)\b')
CAMINHO_VAR_WWW = re.compile(r'''(/var/www/[^\s'";|&<>():/]+)''')
_inventarios = {}  # arquivo -> {'mtime', 'hosts', 'caminhos'}


def mapa_do_inventario(arquivo, repo_do_caminho):
    """({host ou ip: [repo local]}, {caminho remoto: repo local}), relido quando o inventario muda."""
    try:
        mtime = os.stat(arquivo).st_mtime
    except OSError:
        return {}, {}
    cache = _inventarios.get(arquivo)
    if cache is None or cache['mtime'] != mtime:
        hosts, caminhos = {}, {}
        try:
            with open(arquivo) as arq:
                dados = json.load(arq)
            for host in (dados.get('hosts') or {}).values():
                repos = []
                for chave, remoto in (host.get('caminhos') or {}).items():
                    repo = repo_do_caminho.get(chave)
                    if repo and isinstance(remoto, str):
                        caminhos[remoto.rstrip('/')] = repo
                        repos.append(repo)
                nomes = {str(host.get('ssh') or '').rpartition('@')[2], str(host.get('ip') or '')}
                for nome in nomes - {''}:
                    hosts[nome] = repos
        except (OSError, ValueError, AttributeError):
            hosts, caminhos = {}, {}
        cache = _inventarios[arquivo] = {'mtime': mtime, 'hosts': hosts, 'caminhos': caminhos}
    return cache['hosts'], cache['caminhos']


def pastas_remotas(comando, remotos=None):
    """Repos locais correspondentes ao trabalho no servidor: /var/www/<pasta> citado no comando ou host de
    ssh/scp/rsync que so tem um repo (host com varios repos: so o caminho desempata)."""
    remotos = REMOTOS if remotos is None else remotos
    if not remotos or not comando or '/var/www/' not in comando and not COMANDO_REMOTO.search(comando):
        return []
    saida = []
    for r in remotos:
        hosts, caminhos = mapa_do_inventario(r['inventario'], r['repoDoCaminho'])
        repos = [caminhos[p] for p in CAMINHO_VAR_WWW.findall(comando) if p in caminhos]
        if not repos and COMANDO_REMOTO.search(comando):
            for nome, do_host in hosts.items():
                if len(do_host) == 1 and re.search(r'@%s\b' % re.escape(nome), comando):
                    repos.append(do_host[0])
        saida += [os.path.join(r['pasta'], repo) for repo in repos]
    return saida[:10]


def origem_humana(j):
    """`origin.kind` diferente de human (task-notification, mensagens de agentes) nao e prompt do usuario."""
    origem = j.get('origin')
    return not isinstance(origem, dict) or origem.get('kind') in (None, 'human')


def prompt_humano(j):
    """Texto do prompt real do usuario numa linha ja lida, ou None."""
    msg = j.get('message') if isinstance(j, dict) else None
    if not isinstance(msg, dict) or j.get('type') != 'user' or j.get('isMeta') or j.get('isSidechain') or not origem_humana(j):
        return None
    return texto_do_usuario(msg.get('content'))


def texto_do_usuario(conteudo):
    """Texto digitado pelo usuario. Ignora tool_result e os envelopes de comando do harness."""
    if isinstance(conteudo, str):
        textos = [conteudo]
    elif isinstance(conteudo, list):
        if any(isinstance(b, dict) and b.get('type') == 'tool_result' for b in conteudo):
            return None
        textos = [b.get('text') or '' for b in conteudo if isinstance(b, dict) and b.get('type') == 'text']
    else:
        return None
    textos = [t for t in textos if t.strip() and not t.lstrip().startswith('<')]
    return resumir(' '.join(textos)) if textos else None


class Transcrito:
    """Estado de um .jsonl de sessao, lido do final e depois de forma incremental."""

    def __init__(self, caminho, leitura_inicial=LEITURA_ABERTA):
        self.caminho = caminho
        self.leitura_inicial = leitura_inicial
        self.offset = None
        self.resto = b''
        self.mtime = 0
        self.titulo_nome = None  # dado pelo usuario (renomear a aba)
        self.titulo_ia = None
        self.primeiro_prompt = None
        self.ultimo_prompt = None
        self.ultima_resposta = None
        self.parada = None
        self.cwd_inicial = None
        self.cwd_atual = None
        self.branch = None
        self.custo = None
        self.inicio_sessao = None  # menor timestamp visto
        self.inicio_turno = None  # ultimo prompt real do usuario
        self.inicio_leitura = 0  # onde comecou a leitura inicial (0 = arquivo inteiro)
        self.primeiro_ts_cauda = None  # primeiro timestamp da janela lida (inicio aproximado do turno)
        self._cabeca = False
        self._procurou = False
        self.fim_turno = None  # ultima resposta do assistente com end_turn
        self.prs = {}
        self.arquivos = collections.deque(maxlen=200)  # (caminho, peso, e_arquivo) dos ultimos tocados

    def atualizar(self):
        try:
            estado = os.stat(self.caminho)
        except FileNotFoundError:
            return
        self.mtime = estado.st_mtime
        if self.offset is not None and estado.st_size < self.offset:  # reescrito
            self.__init__(self.caminho, self.leitura_inicial)
        descartar_primeira = False
        if self.offset is None:
            inicio = max(0, estado.st_size - self.leitura_inicial)
            self.inicio_leitura = inicio
            if inicio > 0:
                self._ler_cabeca()
                descartar_primeira = True
            self.offset = inicio
        if estado.st_size == self.offset:
            return
        with open(self.caminho, 'rb') as arq:
            arq.seek(self.offset)
            bloco = arq.read(estado.st_size - self.offset)
        self.offset += len(bloco)
        linhas = (self.resto + bloco).split(b'\n')
        self.resto = linhas.pop()  # linha incompleta fica para a proxima leitura
        if descartar_primeira and linhas:
            linhas.pop(0)  # comecou no meio de uma linha
        for linha in linhas:
            if linha.strip():
                self._processar(linha)

    def _ler_cabeca(self):
        """Primeiro prompt e cwd de abertura, que o final do arquivo nao tem."""
        with open(self.caminho, 'rb') as arq:
            linhas = arq.read(LEITURA_CABECA).split(b'\n')[:-1]
        self._cabeca = True  # a cabeca nao diz nada sobre o turno atual (so primeiro prompt e cwd)
        try:
            for linha in linhas:
                self._processar(linha)
                if self.primeiro_prompt:
                    break
        finally:
            self._cabeca = False

    def garantir_inicio_turno(self):
        """Cauda sem prompt real do usuario: procura para tras, em blocos, so por ele (ate ~32 MB).

        Notificacoes de tarefa, mensagens de SendMessage e resultados de ferramenta nao contam."""
        if self.inicio_turno is not None or self._procurou or self.inicio_leitura <= 0:
            return
        self._procurou = True
        fim, lido, sobra = self.inicio_leitura, 0, b''
        try:
            with open(self.caminho, 'rb') as arq:
                while fim > 0 and lido < MAX_BUSCA_PROMPT:
                    ini = max(0, fim - BLOCO_BUSCA_PROMPT)
                    arq.seek(ini)
                    linhas = (arq.read(fim - ini) + sobra).split(b'\n')
                    sobra = linhas.pop(0) if ini > 0 else b''  # pedaco de linha: se completa no bloco anterior
                    for linha in reversed(linhas):
                        # filtro barato antes do json.loads: tool_result e a maioria das linhas "user"
                        if b'"user"' not in linha or b'"type":"tool_result"' in linha or b'"type": "tool_result"' in linha:
                            continue
                        try:
                            j = json.loads(linha)
                        except ValueError:
                            continue
                        texto = prompt_humano(j)
                        if texto:
                            self.inicio_turno = epoch(j.get('timestamp'))
                            self.ultimo_prompt = self.ultimo_prompt or texto
                            return
                    lido += fim - ini
                    fim = ini
        except OSError:
            pass

    def _processar(self, linha):
        try:
            j = json.loads(linha)
        except ValueError:
            return
        if not isinstance(j, dict):
            return
        tipo = j.get('type')
        instante = epoch(j.get('timestamp'))
        if instante is not None and not self._cabeca and self.primeiro_ts_cauda is None:
            self.primeiro_ts_cauda = instante
        if instante is not None and (self.inicio_sessao is None or instante < self.inicio_sessao):
            self.inicio_sessao = instante
        if tipo == 'custom-title':
            self.titulo_nome = j.get('customTitle') or self.titulo_nome
        elif tipo == 'ai-title':
            self.titulo_ia = j.get('aiTitle') or self.titulo_ia
        elif tipo == 'last-prompt':
            self.ultimo_prompt = resumir(j.get('lastPrompt')) or self.ultimo_prompt
        elif tipo == 'pr-link' and j.get('prUrl'):
            self.prs[j['prUrl']] = {'url': j['prUrl'], 'numero': j.get('prNumber'), 'repo': j.get('prRepository')}
        elif tipo == 'cost-state':
            try:
                self.custo = float(j.get('totalCostUSD'))
            except (TypeError, ValueError):
                pass
        if j.get('cwd') and not j.get('isSidechain'):
            self.cwd_inicial = self.cwd_inicial or j['cwd']
            self.cwd_atual = j['cwd']
            self.branch = j.get('gitBranch') or self.branch
        msg = j.get('message')
        if not isinstance(msg, dict) or j.get('isSidechain'):
            return
        if tipo == 'user' and not j.get('isMeta') and origem_humana(j):
            texto = texto_do_usuario(msg.get('content'))
            if texto:
                self.primeiro_prompt = self.primeiro_prompt or texto
                self.ultimo_prompt = texto
                if not self._cabeca:
                    self.inicio_turno = instante or self.inicio_turno
        elif tipo == 'assistant':
            if msg.get('stop_reason'):
                self.parada = msg['stop_reason']
                if msg['stop_reason'] == 'end_turn':
                    self.fim_turno = instante or self.fim_turno
            conteudo = msg.get('content') if isinstance(msg.get('content'), list) else []
            textos = [b.get('text') or '' for b in conteudo if b.get('type') == 'text']
            if any(t.strip() for t in textos):
                self.ultima_resposta = resumir(' '.join(textos))
            for bloco in conteudo:
                if bloco.get('type') != 'tool_use' or not isinstance(bloco.get('input'), dict):
                    continue
                entrada = bloco['input']
                caminho = entrada.get('file_path') or entrada.get('notebook_path') or entrada.get('path')
                if caminho:
                    peso = 3 if bloco.get('name') in ('Edit', 'Write', 'NotebookEdit') else 1
                    self.arquivos.append((caminho, peso, True))
                if bloco.get('name') == 'Bash':
                    comando = entrada.get('command')
                    for pasta in pastas_do_comando(comando, self.cwd_atual) + pastas_remotas(comando):
                        self.arquivos.append((pasta, 1, False))

    def titulo(self):
        return self.titulo_nome or self.titulo_ia

    def subprojeto(self, raiz):
        """Pasta abaixo da raiz em que a sessao trabalha: cwd atual, arquivos tocados, repo do PR."""
        atual = segmento(self.cwd_atual, raiz)
        if atual:
            return atual
        contagem = collections.Counter()
        for caminho, peso, e_arquivo in self.arquivos:
            seg = segmento(caminho, raiz, e_arquivo)
            if seg:
                contagem[seg] += peso
        if contagem:
            return contagem.most_common(1)[0][0]
        for pr in self.prs.values():
            if pr.get('repo'):
                return pr['repo'].rsplit('/', 1)[-1]
        return None


class Painel:
    def __init__(self, base, dias):
        self.base = base
        self.dias = dias
        self.transcritos = {}
        self.caminhos = {}  # id da sessao -> transcrito
        self.listado_em = 0
        self.historico = {}  # id da sessao -> prompts do history.jsonl (sessoes do terminal)
        self.historico_mtime = None
        self.trava = threading.Lock()

    # --- fontes -----------------------------------------------------------

    def sessoes_abertas(self):
        abertas = {}
        for caminho in glob.glob(os.path.join(self.base, 'sessions', '*.json')):
            try:
                with open(caminho) as arq:
                    reg = json.load(arq)
            except (OSError, ValueError):
                continue
            sid = reg.get('sessionId')
            if sid and processo_vivo(reg.get('pid'), reg.get('procStart')):
                abertas[sid] = reg
        return abertas

    def _listar_transcritos(self, agora):
        if agora - self.listado_em < RELISTAR_ARQUIVOS:
            return
        limite = agora - self.dias * 86400
        caminhos = {}
        for caminho in glob.glob(os.path.join(self.base, 'projects', '*', '*.jsonl')):
            try:
                if os.path.getmtime(caminho) >= limite:
                    caminhos[os.path.basename(caminho)[:-6]] = caminho
            except OSError:
                pass
        self.caminhos = caminhos
        self.listado_em = agora

    def _transcrito(self, sid, aberta):
        caminho = self.caminhos.get(sid)
        if caminho is None and aberta:  # sessao nova, ainda fora da ultima listagem
            achados = glob.glob(os.path.join(self.base, 'projects', '*', sid + '.jsonl'))
            caminho = self.caminhos[sid] = achados[0] if achados else None
        if caminho is None:
            return None
        t = self.transcritos.get(caminho)
        if t is None:
            t = self.transcritos[caminho] = Transcrito(caminho, LEITURA_ABERTA if aberta else LEITURA_FECHADA)
        t.atualizar()
        if aberta:
            t.garantir_inicio_turno()
        return t

    def atencao(self, sid, transcrito):
        """Alerta do hook ainda valido: o transcrito nao foi escrito depois dele."""
        try:
            with open(os.path.join(self.base, 'state', 'atencao', sid + '.json')) as arq:
                alerta = json.load(arq)
        except (OSError, ValueError):
            return None
        if transcrito and transcrito.mtime > (alerta.get('ts') or 0) + FOLGA_ATENCAO:
            return None
        return alerta

    def _carregar_historico(self):
        caminho = os.path.join(self.base, 'history.jsonl')
        try:
            mtime = os.path.getmtime(caminho)
        except OSError:
            return
        if mtime == self.historico_mtime:
            return
        historico = collections.defaultdict(list)
        with open(caminho, errors='replace') as arq:
            for linha in arq:
                try:
                    j = json.loads(linha)
                except ValueError:
                    continue
                if j.get('sessionId') and j.get('display'):
                    historico[j['sessionId']].append(j['display'])
        self.historico = historico
        self.historico_mtime = mtime

    def _subagentes_rodando(self, transcrito, agora):
        if not transcrito:
            return 0
        pasta = os.path.join(transcrito.caminho[:-6], 'subagents')
        return sum(1 for c in glob.glob(os.path.join(pasta, '*.jsonl'))
                   if agora - os.path.getmtime(c) < SEGUNDOS_SUBAGENTE)

    # --- estado -----------------------------------------------------------

    def estado(self):
        with self.trava:
            agora = time.time()
            self._listar_transcritos(agora)
            self._carregar_historico()
            abertas = self.sessoes_abertas()
            sessoes = [self._montar(sid, reg, agora) for sid, reg in abertas.items()]
            fechadas = []
            for sid in self.caminhos:
                if sid in abertas:
                    continue
                t = self._transcrito(sid, False)
                if t and (t.primeiro_prompt or t.ultimo_prompt or t.titulo()):
                    fechadas.append((t.mtime, sid))
            fechadas.sort(reverse=True)
            sessoes += [self._montar(sid, None, agora) for _, sid in fechadas[:MAX_FECHADAS]]
            return {'geradoEm': agora, 'dias': self.dias, 'sessoes': sessoes}

    def _montar(self, sid, reg, agora):
        aberta = reg is not None
        t = self._transcrito(sid, aberta)
        raiz = (reg or {}).get('cwd') or (t.cwd_inicial if t else None)
        alerta = self.atencao(sid, t) if aberta else None
        status = (reg or {}).get('status')
        if not aberta:
            coluna = 'fechada'
        elif alerta and alerta.get('tipo') == 'permission_prompt':
            coluna = 'atencao'
        elif status == 'busy':
            coluna = 'trabalhando'
        elif status not in (None, 'idle'):
            coluna = 'atencao'  # estado que o painel nao conhece: melhor olhar
        else:
            coluna = 'suaVez'
        ultima = max(t.mtime if t else 0, ((reg or {}).get('updatedAt') or 0) / 1000)
        return {
            'id': sid,
            'aberta': aberta,
            'coluna': coluna,
            'status': status,
            'nome': (reg or {}).get('name'),
            'titulo': t.titulo() if t else None,
            'raiz': raiz,
            'projeto': os.path.basename(raiz.rstrip(os.sep)) if raiz else None,
            'subprojeto': t.subprojeto(raiz) if t else None,
            'cwd': (t.cwd_atual if t else None) or raiz,
            'branch': t.branch if t else None,
            'entrada': (reg or {}).get('entrypoint'),
            'tipo': (reg or {}).get('kind'),
            'jobId': (reg or {}).get('jobId'),  # so sessao --bg: id do attach/logs/stop
            'atencao': alerta,
            'nova': coluna == 'suaVez' and t is not None and t.parada == 'end_turn'
                    and agora - t.mtime < RESPOSTA_NOVA,
            'primeiroPrompt': t.primeiro_prompt if t else None,
            'ultimoPrompt': t.ultimo_prompt if t else None,
            'ultimaResposta': t.ultima_resposta if t else None,
            'prs': list(t.prs.values())[-3:] if t else [],
            'custo': t.custo if t else None,
            'subagentesRodando': self._subagentes_rodando(t, agora) if aberta else 0,
            'ultimaAtividade': ultima,
            'inicioSessao': t.inicio_sessao if t else None,
            'inicioTurno': t.inicio_turno if t else None,
            # cauda sem prompt achado: o turno comecou NA JANELA lida ou antes ("rodando ha mais de X")
            'inicioTurnoAproximado': (t.primeiro_ts_cauda if t and t.inicio_turno is None and t.inicio_leitura > 0 else None),
            'fimTurno': t.fim_turno if t else None,
            'historico': resumir(' | '.join(self.historico.get(sid, [])[-20:]), 2000),
        }


def validar_nova_sessao(dados, home):
    """(argv, cwd) do `claude --bg`, ou levanta ValueError com o motivo."""
    if not isinstance(dados, dict):
        raise ValueError('corpo invalido')
    prompt = str(dados.get('prompt') or '').strip()
    if not prompt or len(prompt) > MAX_PROMPT:
        raise ValueError('prompt vazio ou longo demais')
    pasta = os.path.realpath(os.path.expanduser(str(dados.get('cwd') or '')))
    if not os.path.isdir(pasta) or not (pasta + os.sep).startswith(os.path.realpath(home) + os.sep):
        raise ValueError('pasta inexistente ou fora da home')
    argv = ['claude', '--bg']
    nome = ' '.join(str(dados.get('nome') or '').split())[:60]
    if nome:
        argv += ['--name', nome]
    return argv + ['--', prompt], pasta


def host_permitido(host, porta):
    """Bloqueia DNS rebinding: so aceita o painel chamado pelo proprio endereco local."""
    return host in ('127.0.0.1:%d' % porta, 'localhost:%d' % porta)


def origem_permitida(origem, porta):
    """POST so da propria pagina. Sem Origin (curl) passa: o token ja barra o resto."""
    return not origem or origem in ('http://127.0.0.1:%d' % porta, 'http://localhost:%d' % porta)
