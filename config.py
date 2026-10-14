"""Configuracao do Mapa de agentes: ~/.config/mapa-agentes/config.json (opcional).

Sem arquivo, cada pasta de primeiro nivel de `pastaProjetos` (padrao ~/dev) vira uma regiao avulsa
no mapa. Um WORKSPACE e uma pasta que agrupa varios projetos: ganha regiao grande com contorno,
botao de foco na barra, e cada subpasta dela vira um no. Exemplo em config.exemplo.json:

{
  "pastaProjetos": "~/dev",
  "workspaces": [
    {"id": "empresa", "nome": "Empresa", "pasta": "~/dev/empresa", "cor": "#b478ff"},
    {"id": "loja", "nome": "Loja", "pasta": "~/dev/loja-workspace", "cor": "#46a0ff",
     "irmaos": "loja-*", "nosFixos": "loja-*",
     "remoto": {"inventario": ".claude/inventario.json", "repoDoCaminho": {"laravel": "loja-app"}}}
  ]
}

- irmaos: pastas de `pastaProjetos` que pertencem ao workspace como no proprio (worktrees, copias);
- nosFixos: subpastas do workspace com .git que aparecem mesmo sem sessao (para abrir sessao ali);
- apelidos: outros nomes de uma subpasta, para reconhecer o projeto pelo titulo ou pelo prompt da
  sessao ({"loja-virtual": ["acme", "cliente-x"]}); o nome da propria pasta ja vale sem apelido;
- remoto: inventario de servidores (hosts e caminhos em /var/www) para reconhecer o repo de uma
  sessao que trabalha por ssh. Caminho relativo a pasta do workspace.

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


def expandir(caminho, home):
    """~ relativo a `home` (e nao a home do processo): o mesmo config vale em testes e em outra conta."""
    caminho = caminho.strip()
    if caminho == '~' or caminho.startswith('~/'):
        caminho = home + caminho[1:]
    return os.path.normpath(caminho)


class Workspace:
    def __init__(self, dados, home, indice):
        if not isinstance(dados, dict):
            raise ValueError('cada workspace deve ser um objeto')
        self.id = dados.get('id')
        if not isinstance(self.id, str) or not ID_VALIDO.match(self.id) or self.id in RESERVADOS:
            raise ValueError('workspace com id invalido: %r (minusculas, numeros, - e _)' % self.id)
        pasta = dados.get('pasta')
        if not isinstance(pasta, str) or not pasta.strip():
            raise ValueError('workspace %s sem "pasta"' % self.id)
        self.pasta = expandir(pasta, home)
        if not (self.pasta + os.sep).startswith(os.path.normpath(home) + os.sep):
            raise ValueError('workspace %s: a pasta precisa estar dentro da home' % self.id)
        self.nome = str(dados.get('nome') or self.id)[:40]
        cor = dados.get('cor') or PALETA[indice % len(PALETA)]
        if not isinstance(cor, str) or not COR_VALIDA.match(cor):
            raise ValueError('workspace %s: cor deve ser #rrggbb' % self.id)
        self.cor = cor.lower()
        self.irmaos = self._padrao(dados, 'irmaos')
        self.nos_fixos = self._padrao(dados, 'nosFixos')
        apelidos = dados.get('apelidos') or {}
        if not isinstance(apelidos, dict) or not all(isinstance(v, list) and all(isinstance(x, str) for x in v) for v in apelidos.values()):
            raise ValueError('workspace %s: "apelidos" deve ser {"subpasta": ["apelido", ...]}' % self.id)
        self.apelidos = {str(k): list(v) for k, v in apelidos.items()}
        self.remoto = None
        remoto = dados.get('remoto')
        if remoto is not None:
            if not isinstance(remoto, dict) or not isinstance(remoto.get('repoDoCaminho'), dict):
                raise ValueError('workspace %s: "remoto" precisa de "inventario" e "repoDoCaminho"' % self.id)
            inventario = os.path.join(self.pasta, expandir(str(remoto.get('inventario') or '.claude/inventario.json'), home))
            self.remoto = {'pasta': self.pasta, 'inventario': inventario,
                           'repoDoCaminho': {str(k): str(v) for k, v in remoto['repoDoCaminho'].items()}}

    def _padrao(self, dados, chave):
        valor = dados.get(chave)
        if valor is None:
            return None
        if not isinstance(valor, str) or '/' in valor or not valor.strip():
            raise ValueError('workspace %s: "%s" deve ser um padrao de nome de pasta (ex.: projeto-*)' % (self.id, chave))
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
            raise ValueError('o config deve ser um objeto JSON')
        pasta = dados.get('pastaProjetos') or '~/dev'
        if not isinstance(pasta, str):
            raise ValueError('"pastaProjetos" deve ser um caminho')
        self.pasta_projetos = expandir(pasta, self.home)
        lista = dados.get('workspaces') or []
        if not isinstance(lista, list):
            raise ValueError('"workspaces" deve ser uma lista')
        self.workspaces = [Workspace(w, self.home, i) for i, w in enumerate(lista)]
        ids = [w.id for w in self.workspaces]
        if len(set(ids)) != len(ids):
            raise ValueError('ids de workspace repetidos')
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
        raise ValueError('%s nao e JSON valido: %s' % (caminho, e))
    return Config(dados, home)
