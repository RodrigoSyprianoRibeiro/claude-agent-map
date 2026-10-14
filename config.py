"""Configuracao do Mapa de agentes: ~/.config/mapa-agentes/config.json (opcional).

Sem arquivo, cada pasta de primeiro nivel de `projectsDir` (padrao ~/dev) vira uma regiao avulsa
no mapa. Um WORKSPACE e uma pasta que agrupa varios projetos: ganha regiao grande com contorno,
botao de foco na barra, e cada subpasta dela vira um no. Exemplo em config.example.json:

{
  "projectsDir": "~/dev",
  "workspaces": [
    {"id": "acme", "name": "Acme", "path": "~/dev/acme", "color": "#b478ff"},
    {"id": "shop", "name": "Shop", "path": "~/dev/shop-workspace", "color": "#46a0ff",
     "siblings": "shop-*", "pinned": "shop-*",
     "remote": {"inventory": ".claude/inventory.json", "repoByPath": {"laravel": "shop-app"}}}
  ]
}

- siblings: pastas de `projectsDir` que pertencem ao workspace como no proprio (worktrees, copias);
- pinned: subpastas do workspace com .git que aparecem mesmo sem sessao (para abrir sessao ali);
- aliases: outros nomes de uma subpasta, para reconhecer o projeto pelo titulo ou pelo prompt da
  sessao ({"online-store": ["acme", "client-x"]}); o nome da propria pasta ja vale sem apelido;
- remote: inventario de servidores (hosts e caminhos em /var/www) para reconhecer o repo de uma
  sessao que trabalha por ssh. Caminho relativo a pasta do workspace.

As chaves tambem valem em portugues (pastaProjetos, nome, pasta, cor, apelidos, irmaos, nosFixos,
remoto, inventario, repoDoCaminho): e o formato original, mantido por compatibilidade.

So biblioteca padrao.
"""

import fnmatch
import json
import os
import re

ARQUIVO = os.path.join(os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config'), 'mapa-agentes', 'config.json')
ID_VALIDO = re.compile(r'^[a-z0-9][a-z0-9_-]{0,39}$')
COR_VALIDA = re.compile(r'^#[0-9a-fA-F]{6}$')
RESERVADOS = {'home', 'geral'}
PALETA = ('#b478ff', '#46a0ff', '#3ddc97', '#ffb547', '#ff6b9a', '#5ad1e6')


# chaves em ingles -> nome interno (o formato original era em portugues e continua valendo)
CHAVES_RAIZ = {'projectsDir': 'pastaProjetos'}
CHAVES_WORKSPACE = {'name': 'nome', 'path': 'pasta', 'color': 'cor', 'aliases': 'apelidos', 'siblings': 'irmaos',
                    'pinned': 'nosFixos', 'remote': 'remoto'}
CHAVES_REMOTO = {'inventory': 'inventario', 'repoByPath': 'repoDoCaminho'}


def normalizar_chaves(dados, mapa, onde):
    """Copia `dados` trocando as chaves em ingles pelas internas. As duas formas juntas = erro."""
    if not isinstance(dados, dict):
        return dados
    saida = dict(dados)
    for en, pt in mapa.items():
        if en in saida:
            if pt in saida:
                raise ValueError('%s: use "%s" or "%s", not both' % (onde, en, pt))
            saida[pt] = saida.pop(en)
    return saida


def expandir(caminho, home):
    """~ relativo a `home` (e nao a home do processo): o mesmo config vale em testes e em outra conta."""
    caminho = caminho.strip()
    if caminho == '~' or caminho.startswith('~/'):
        caminho = home + caminho[1:]
    return os.path.normpath(caminho)


class Workspace:
    def __init__(self, dados, home, indice):
        if not isinstance(dados, dict):
            raise ValueError('each workspace must be a JSON object')
        self.id = dados.get('id')
        dados = normalizar_chaves(dados, CHAVES_WORKSPACE, 'workspace %s' % self.id)
        if not isinstance(self.id, str) or not ID_VALIDO.match(self.id) or self.id in RESERVADOS:
            raise ValueError('invalid workspace id: %r (lowercase letters, digits, - and _)' % self.id)
        pasta = dados.get('pasta')
        if not isinstance(pasta, str) or not pasta.strip():
            raise ValueError('workspace %s: missing "path"' % self.id)
        self.pasta = expandir(pasta, home)
        if not (self.pasta + os.sep).startswith(os.path.normpath(home) + os.sep):
            raise ValueError('workspace %s: "path" must be inside your home directory' % self.id)
        self.nome = str(dados.get('nome') or self.id)[:40]
        cor = dados.get('cor') or PALETA[indice % len(PALETA)]
        if not isinstance(cor, str) or not COR_VALIDA.match(cor):
            raise ValueError('workspace %s: "color" must be #rrggbb' % self.id)
        self.cor = cor.lower()
        self.irmaos = self._padrao(dados, 'irmaos')
        self.nos_fixos = self._padrao(dados, 'nosFixos')
        apelidos = dados.get('apelidos') or {}
        if not isinstance(apelidos, dict) or not all(isinstance(v, list) and all(isinstance(x, str) for x in v) for v in apelidos.values()):
            raise ValueError('workspace %s: "aliases" must be {"subfolder": ["alias", ...]}' % self.id)
        self.apelidos = {str(k): list(v) for k, v in apelidos.items()}
        self.remoto = None
        remoto = normalizar_chaves(dados.get('remoto'), CHAVES_REMOTO, 'workspace %s remote' % self.id)
        if remoto is not None:
            if not isinstance(remoto, dict) or not isinstance(remoto.get('repoDoCaminho'), dict):
                raise ValueError('workspace %s: "remote" needs "inventory" and "repoByPath"' % self.id)
            inventario = os.path.join(self.pasta, expandir(str(remoto.get('inventario') or '.claude/inventario.json'), home))
            self.remoto = {'pasta': self.pasta, 'inventario': inventario,
                           'repoDoCaminho': {str(k): str(v) for k, v in remoto['repoDoCaminho'].items()}}

    def _padrao(self, dados, chave):
        valor = dados.get(chave)
        if valor is None:
            return None
        if not isinstance(valor, str) or '/' in valor or not valor.strip():
            raise ValueError('workspace %s: "%s" must be a folder name pattern (e.g. project-*)' % (self.id, {'irmaos': 'siblings', 'nosFixos': 'pinned'}[chave]))
        return valor

    def e_irmao(self, nome_pasta):
        """Pasta de pastaProjetos que pertence a este workspace (e nao e a propria pasta do workspace)."""
        return bool(self.irmaos) and nome_pasta != os.path.basename(self.pasta) and fnmatch.fnmatch(nome_pasta, self.irmaos)

    def fixos(self, existe_git=None):
        """Subpastas do workspace com .git DIRETORIO que casam com nosFixos (worktree tem .git arquivo)."""
        if not self.nos_fixos:
            return []
        existe_git = existe_git or (lambda p: os.path.isdir(os.path.join(p, '.git')))
        try:
            nomes = os.listdir(self.pasta)
        except OSError:
            return []
        return sorted(n for n in nomes if fnmatch.fnmatch(n, self.nos_fixos) and existe_git(os.path.join(self.pasta, n)))

    def publico(self):
        return {'id': self.id, 'nome': self.nome, 'cor': self.cor}


class Config:
    def __init__(self, dados=None, home=None):
        self.home = os.path.normpath(home or os.path.expanduser('~'))
        dados = {} if dados is None else dados
        if not isinstance(dados, dict):
            raise ValueError('the config must be a JSON object')
        dados = normalizar_chaves(dados, CHAVES_RAIZ, 'config')
        pasta = dados.get('pastaProjetos') or '~/dev'
        if not isinstance(pasta, str):
            raise ValueError('"projectsDir" must be a path')
        self.pasta_projetos = expandir(pasta, self.home)
        lista = dados.get('workspaces') or []
        if not isinstance(lista, list):
            raise ValueError('"workspaces" must be a list')
        self.workspaces = [Workspace(w, self.home, i) for i, w in enumerate(lista)]
        ids = [w.id for w in self.workspaces]
        if len(set(ids)) != len(ids):
            raise ValueError('duplicate workspace ids')
        self.por_id = {w.id: w for w in self.workspaces}

    @property
    def remotos(self):
        return [w.remoto for w in self.workspaces if w.remoto]

    def workspace_da_pasta(self, caminho):
        """Workspace cuja pasta contem `caminho` (o mais especifico), ou None."""
        caminho = os.path.normpath(caminho)
        achados = [w for w in self.workspaces if caminho == w.pasta or caminho.startswith(w.pasta + os.sep)]
        return max(achados, key=lambda w: len(w.pasta)) if achados else None


def carregar(caminho=ARQUIVO, home=None):
    """Config do arquivo, ou o padrao se ele nao existir. Arquivo invalido levanta ValueError com o motivo."""
    try:
        with open(caminho) as arq:
            dados = json.load(arq)
    except FileNotFoundError:
        return Config(home=home)
    except ValueError as e:
        raise ValueError('%s is not valid JSON: %s' % (caminho, e))
    return Config(dados, home)
