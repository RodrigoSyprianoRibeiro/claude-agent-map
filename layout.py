"""Layout do mapa de agentes: logica pura, sem rede. Regioes estaveis, nos em slots estaveis.

Cada workspace do config.json e uma regiao grande (em fila, na ordem do arquivo) e cada subpasta
dele vira um no. Todo outro projeto de `pastaProjetos` e uma regiao avulsa, numa fila abaixo. A posicao de um no novo e o proximo slot livre de uma
espiral em aneis dentro da regiao e fica gravada em disco: aparecer no novo nunca move os
existentes, e a posicao arrastada pelo usuario vale sempre.
"""

import difflib
import json
import math
import os
import re
import tempfile
import threading
import time
import unicodedata

import config as configuracao

DIAS_VISIVEL = 14
PASSO = 440  # distancia entre aneis; vizinhos no mesmo anel ficam a ~1.05*PASSO (min. 150 de mundo)
MARGEM_REGIAO = 280
LIMITE_COORD = 50_000

RAIO_GRANDE = 480  # raio minimo da regiao de um workspace
RAIO_MIN_AVULSO = 250
FOLGA_REGIOES = 140  # espaco fixo entre os contornos
LARGURA_MIN_FILA = 2400  # largura minima da fila de avulsos (sem workspace, ou com um so pequeno)


def centros_das_grandes(raios, ordem):
    """Workspaces lado a lado, na ordem do config, com folga fixa entre os contornos.

    Devolve (centros, largura, base): largura da fila e o maior raio (a fila de avulsos fica abaixo)."""
    if not ordem:
        return {}, 0.0, -FOLGA_REGIOES
    largura = sum(2 * raios[r] for r in ordem) + FOLGA_REGIOES * (len(ordem) - 1)
    centros, x = {}, -largura / 2
    for r in ordem:
        centros[r] = (x + raios[r], 0.0)
        x += 2 * raios[r] + FOLGA_REGIOES
    return centros, largura, max(raios[r] for r in ordem)


def centros_dos_avulsos(avulsos, largura, base):
    """Fila abaixo das grandes: avulsos em ordem de slot, quebrando linha na largura das grandes (ou na minima).

    avulsos: lista [(id, raio)] ja ordenada por slot."""
    centros = {}
    largura = max(largura, LARGURA_MIN_FILA)
    x0 = -largura / 2
    y_topo = base + FOLGA_REGIOES
    x, altura_linha = x0, 0.0
    for rid, raio in avulsos:
        if x > x0 and x + 2 * raio > x0 + largura:
            x, y_topo, altura_linha = x0, y_topo + altura_linha + FOLGA_REGIOES / 2, 0.0
        centros[rid] = (x + raio, y_topo + raio)
        x += 2 * raio + FOLGA_REGIOES / 2
        altura_linha = max(altura_linha, 2 * raio)
    return centros


LIMIAR_TEXTO = 0.88  # "lojavirtal" ~ loja-virtual (0.95); palavras so parecidas ficam abaixo de 0.8


def normalizar(texto):
    """minusculas e sem acento: 'Atualizações' -> 'atualizacoes'."""
    return unicodedata.normalize('NFKD', str(texto or '')).encode('ascii', 'ignore').decode().lower()


def _formas(texto):
    """Sequencias de 1 a 3 palavras do texto, coladas: 'portal vagas' -> {'portal', 'vagas', 'portalvagas'}."""
    palavras = re.findall(r'[a-z0-9]+', normalizar(texto)[:300])
    return {''.join(palavras[i:i + n]) for n in (1, 2, 3) for i in range(len(palavras) - n + 1)}


def projeto_pelo_texto(textos, subpastas, apelidos=None):
    """Subpasta citada no titulo ou no prompt, ou None. Aceita erro de digitacao em nomes longos e apelidos.

    Os textos sao tentados em ordem (titulo antes do prompt); texto que cita mais de uma pasta = None."""
    alvos = {}
    for pasta in subpastas:
        alvos.setdefault(re.sub(r'[^a-z0-9]', '', normalizar(pasta)), pasta)
    for pasta, lista in (apelidos or {}).items():
        if pasta in subpastas:
            for apelido in lista:
                alvos.setdefault(re.sub(r'[^a-z0-9]', '', normalizar(apelido)), pasta)
    alvos = {f: p for f, p in alvos.items() if len(f) >= 4}
    for texto in textos:
        if not texto:
            continue
        formas = _formas(texto)
        notas = {}
        for forma, pasta in alvos.items():
            if forma in formas:
                nota = 1.0
            elif len(forma) >= 6:
                nota = max((difflib.SequenceMatcher(None, forma, f).ratio() for f in formas if abs(len(f) - len(forma)) <= 3), default=0)
            else:
                continue
            if nota >= LIMIAR_TEXTO:
                notas[pasta] = max(notas.get(pasta, 0), nota)
        if notas:  # mais de um projeto citado: melhor ficar em 'geral' do que chutar
            return next(iter(notas)) if len(notas) == 1 else None
    return None


def parse_foco(valor):
    """'empresa' ou 'empresa/portal' -> (regiao, id do no ou None). Vazio -> (None, None).

    Qualquer projeto avulso e uma regiao, entao so a forma e validada; a pagina confere
    se a regiao existe quando carrega. Levanta ValueError para valor mal formado."""
    valor = (valor or '').strip()
    if not valor:
        return None, None
    regiao, _, nome = valor.partition('/')
    if not regiao or regiao.startswith('.') or len(valor) > 200 or any(c in valor for c in '?#&\\\0'):
        raise ValueError('foco invalido: %r (use um workspace, um projeto ou regiao/projeto)' % valor)
    return regiao, (valor if nome else None)


def _sob(caminho, raiz):
    """Primeira pasta de `caminho` abaixo de `raiz`: '' se for a propria raiz, None se estiver fora."""
    if not caminho:
        return None
    caminho = os.path.normpath(caminho)
    raiz = os.path.normpath(raiz)
    if caminho == raiz:
        return ''
    if caminho.startswith(raiz + os.sep):
        return caminho[len(raiz) + 1:].split(os.sep)[0]
    return None


def classificar(sessao, home, existe=os.path.isdir, config=None, inferir=None):
    """(regiao, nome do no) pela RAIZ onde a sessao comecou, ou None se nao deve virar no.

    O subprojeto inferido vem de comandos e arquivos tocados e pode ser lixo (`%s`, `memory`):
    so vale se for uma pasta que existe diretamente abaixo da raiz; senao vai para 'geral'.
    Nunca viram no: caminhos sob ~/.claude e raizes fora da home (ex.: /tmp/.../scratchpad).
    Dentro de um workspace a regiao e o workspace; fora, cada projeto e uma regiao avulsa.
    Sem subprojeto pelos arquivos, `inferir(raiz, sessao, apelidos)` tenta pelo titulo e pelo prompt.
    """
    config = config or configuracao.Config(home=home)
    raiz = sessao.get('raiz') or sessao.get('cwd')
    if not raiz:
        return None
    raiz = os.path.normpath(raiz)
    home = os.path.normpath(home)
    if _sob(raiz, os.path.join(home, '.claude')) is not None:
        return None
    na_home = _sob(raiz, home)
    if na_home is None:
        return None
    if na_home == '':
        return 'home', 'geral'
    sub = sessao.get('subprojeto')
    ws = config.workspace_da_pasta(raiz)
    if ws:
        abaixo = _sob(raiz, ws.pasta)
        if abaixo and not abaixo.startswith('.'):  # raiz ja dentro de um subprojeto
            return ws.id, abaixo
        if sub and not sub.startswith('.') and existe(os.path.join(raiz, sub)):
            return ws.id, sub
        pelo_texto = inferir(raiz, sessao, ws.apelidos) if inferir else None
        return ws.id, pelo_texto or 'geral'
    dev = config.pasta_projetos
    projeto = _sob(raiz, dev)
    if projeto is None:  # dentro da home, fora da pasta de projetos
        return na_home, 'geral'
    if projeto == '':
        return os.path.basename(dev), 'geral'
    for w in config.workspaces:
        if w.e_irmao(projeto):  # worktrees e copias do workspace
            return w.id, projeto
    abaixo = _sob(raiz, os.path.join(dev, projeto))
    if abaixo and not abaixo.startswith('.'):
        return projeto, abaixo
    if sub and not sub.startswith('.') and existe(os.path.join(raiz, sub)):
        return projeto, sub
    pelo_texto = inferir(raiz, sessao, None) if inferir else None
    return projeto, pelo_texto or 'geral'


def pasta_do_no(home, regiao, nome, existe=os.path.isdir, config=None):
    """Pasta real de um no (para abrir sessao nova nele), ou None. O cliente nunca manda caminho."""
    config = config or configuracao.Config(home=home)
    dev = config.pasta_projetos
    ws = config.por_id.get(regiao)
    if regiao == 'home':
        candidatos = [home]
    elif ws:
        candidatos = [ws.pasta] if nome == 'geral' else [os.path.join(ws.pasta, nome), os.path.join(dev, nome)]
    elif regiao == os.path.basename(dev) and nome == 'geral':
        candidatos = [dev]
    else:
        base = os.path.join(dev, regiao)
        candidatos = [base if nome == 'geral' else os.path.join(base, nome), os.path.join(home, regiao)]
        if nome == 'geral':
            candidatos.append(os.path.join(home, regiao))
    for c in candidatos:
        if existe(c):
            return c
    return None


def _subpastas(raiz):
    try:
        return sorted(n for n in os.listdir(raiz) if not n.startswith('.') and os.path.isdir(os.path.join(raiz, n)))
    except OSError:
        return []


def posicao_do_slot(regiao, slot):
    """Deslocamento do slot k em relacao ao centro da regiao: 0 no centro, aneis de 6*r slots a r*PASSO."""
    if slot == 0:
        return 0.0, 0.0
    anel, resto = 1, slot - 1
    while resto >= 6 * anel:
        resto -= 6 * anel
        anel += 1
    angulo = -math.pi / 2 + 2 * math.pi * resto / (6 * anel) + anel * 0.35
    return round(math.cos(angulo) * anel * PASSO, 1), round(math.sin(angulo) * anel * PASSO, 1)


class Layout:
    def __init__(self, caminho_posicoes, home=None, existe=os.path.isdir, config=None, fixos=None, listar=None):
        self.caminho = caminho_posicoes
        self.home = os.path.normpath(home or os.path.expanduser('~'))
        self.existe = existe
        self.config = config or configuracao.Config(home=self.home)
        # () -> [(regiao, nome)] dos nos que aparecem mesmo sem sessao (nosFixos dos workspaces)
        self.fixos = fixos or (lambda: [(w.id, n) for w in self.config.workspaces for n in w.fixos()])
        self.listar = listar or _subpastas
        self._subpastas = {}  # raiz -> (ate, [nomes]): listagem com validade curta
        self._inferidos = {}  # (raiz, titulo, prompt) -> subpasta ou None
        self.trava = threading.Lock()
        self.posicoes, self.avulsos = self._carregar()  # posicoes: x, y RELATIVOS ao centro da regiao
        self.centros = {}

    def inferir(self, raiz, sessao, apelidos):
        """Subpasta de `raiz` citada no titulo ou no primeiro prompt (com cache: roda a cada estado)."""
        chave = (raiz, sessao.get('titulo'), sessao.get('primeiroPrompt'))
        if chave not in self._inferidos:
            agora = time.monotonic()
            ate, nomes = self._subpastas.get(raiz, (0, None))
            if nomes is None or agora > ate:
                nomes = self.listar(raiz)
                self._subpastas[raiz] = (agora + 60, nomes)
            if len(self._inferidos) > 5000:
                self._inferidos.clear()
            self._inferidos[chave] = projeto_pelo_texto([chave[1], chave[2]], nomes, apelidos)
        return self._inferidos[chave]

    def _carregar(self):
        try:
            with open(self.caminho) as arq:
                dados = json.load(arq)
        except (OSError, ValueError):
            return {}, {}
        if not isinstance(dados, dict):
            return {}, {}
        if isinstance(dados.get('nos'), dict):  # formato atual
            nos, avulsos = dados['nos'], dados.get('avulsos') if isinstance(dados.get('avulsos'), dict) else {}
        else:  # formato antigo: so os nos, na raiz do arquivo
            nos, avulsos = dados, {}
        nos = {k: v for k, v in nos.items() if isinstance(v, dict) and 'x' in v and 'y' in v and 'regiao' in v
               and v['regiao'] != 'outros'}
        return nos, {k: v for k, v in avulsos.items() if isinstance(v, int)}

    def _gravar(self):
        pasta = os.path.dirname(self.caminho)
        os.makedirs(pasta, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=pasta, suffix='.tmp')
        with os.fdopen(fd, 'w') as arq:
            json.dump({'nos': self.posicoes, 'avulsos': self.avulsos}, arq, indent=1, sort_keys=True)
        os.replace(tmp, self.caminho)

    def _slot_livre(self, regiao):
        usados = {p.get('slot') for p in self.posicoes.values() if p['regiao'] == regiao}
        slot = 0
        while slot in usados:
            slot += 1
        return slot

    def definir_posicao(self, chave, x, y):
        """Posicao arrastada (coordenadas de mundo). Guarda relativa ao centro da regiao.

        Devolve False se o no nao e conhecido ou o valor e invalido."""
        try:
            x, y = float(x), float(y)
        except (TypeError, ValueError):
            return False
        if not (math.isfinite(x) and math.isfinite(y)) or abs(x) > LIMITE_COORD or abs(y) > LIMITE_COORD:
            return False
        with self.trava:
            if chave not in self.posicoes:
                return False
            centro = self.centros.get(self.posicoes[chave]['regiao'])
            if centro is None:
                return False
            self.posicoes[chave].update(x=round(x - centro[0], 1), y=round(y - centro[1], 1))
            self._gravar()
        return True

    def montar(self, sessoes, agora):
        """{'regioes', 'nos', 'sessoes'}: sessoes ganham 'no'; nos de sessoes recentes e os repos fixos."""
        limite = agora - DIAS_VISIVEL * 86400
        grupos = {}
        for s in sessoes:
            classe = classificar(s, self.home, self.existe, self.config, self.inferir)
            if classe is None:
                continue
            grupos.setdefault(classe, []).append(s)

        visiveis = {}
        for (regiao, nome), lista in grupos.items():
            if any(s.get('aberta') or (s.get('ultimaAtividade') or 0) >= limite for s in lista):
                visiveis[(regiao, nome)] = lista
        for chave in self.fixos():
            visiveis.setdefault(chave, grupos.get(chave, []))
        grandes = [w.id for w in self.config.workspaces]

        with self.trava:
            mudou = False
            # ordem estavel para nos novos na mesma chamada: 'geral' primeiro (centro), depois alfabetica
            for regiao, nome in sorted(visiveis, key=lambda k: (k[0], k[1] != 'geral', k[1])):
                chave = '%s/%s' % (regiao, nome)
                if chave not in self.posicoes:
                    slot = self._slot_livre(regiao)
                    x, y = posicao_do_slot(regiao, slot)
                    self.posicoes[chave] = {'regiao': regiao, 'slot': slot, 'x': x, 'y': y}
                    mudou = True
            # projeto avulso novo pega o proximo slot livre da fila; os existentes nao se movem
            for regiao in sorted({r for r, _ in visiveis if r not in grandes}):
                if regiao not in self.avulsos:
                    self.avulsos[regiao] = max(self.avulsos.values(), default=-1) + 1
                    mudou = True
            if mudou:
                self._gravar()
            posicoes = {k: dict(v) for k, v in self.posicoes.items()}
            avulsos_slot = dict(self.avulsos)

        presentes = {r for r, _ in visiveis}
        raios = {}
        for rid in sorted(presentes | set(grandes)):
            raio = RAIO_GRANDE if rid in grandes else RAIO_MIN_AVULSO
            for (regiao, nome) in visiveis:
                if regiao == rid:
                    p = posicoes['%s/%s' % (regiao, nome)]
                    raio = max(raio, math.hypot(p['x'], p['y']) + (MARGEM_REGIAO if rid in grandes else 230))
            raios[rid] = round(raio, 1)
        centros, largura, base = centros_das_grandes(raios, grandes)
        ordem_avulsos = sorted((r for r in presentes if r not in grandes), key=lambda r: avulsos_slot[r])
        centros.update(centros_dos_avulsos([(r, raios[r]) for r in ordem_avulsos], largura, base))
        with self.trava:
            self.centros = centros

        nos, saida = [], []
        for (regiao, nome), lista in sorted(visiveis.items()):
            chave = '%s/%s' % (regiao, nome)
            pos = posicoes[chave]
            cx, cy = centros[regiao]
            # o no "geral" de um projeto avulso leva o nome do projeto
            unico = sum(1 for r, _ in visiveis if r == regiao) == 1
            exibido = nome
            if regiao not in grandes and nome == 'geral':
                exibido = regiao
            elif regiao not in grandes and unico:
                exibido = '%s / %s' % (regiao, nome)  # no solitario numa subpasta: sem o projeto o nome some
            nos.append({
                'id': chave, 'regiao': regiao, 'nome': exibido, 'x': round(cx + pos['x'], 1), 'y': round(cy + pos['y'], 1),
                'total': len(lista), 'abertas': sum(1 for s in lista if s.get('aberta')),
                'caminho': pasta_do_no(self.home, regiao, nome, self.existe, self.config),
            })
            for s in lista:
                saida.append(dict(s, no=chave))

        regioes = []
        for rid in grandes + ordem_avulsos:
            n_nos = sum(1 for r, _ in visiveis if r == rid)
            ws = self.config.por_id.get(rid)
            regioes.append({
                'id': rid, 'nome': ws.nome.upper() if ws else rid, 'rotulo': ws.nome if ws else rid,
                'cor': ws.cor if ws else None,
                'x': round(centros[rid][0], 1), 'y': round(centros[rid][1], 1), 'raio': raios[rid],
                'grande': ws is not None, 'contorno': ws is not None or n_nos > 1, 'nNos': n_nos,
            })
        return {'regioes': regioes, 'nos': nos, 'sessoes': saida}
