"""Fluxo oficial de confianca de pasta, dentro do app.

`claude --bg` recusa pasta nao confiavel ("Workspace not trusted"). O app abre `claude` interativo
numa PTY (argv fixo, cwd = pasta do no) para o usuario aceitar o dialogo oficial no teclado; aqui
ficam o detector do desfecho (a partir do que o TUI desenha) e a limpeza do transcrito vazio que
o claude interativo possa ter criado. Nao mexe em nenhum arquivo interno do Claude.
So biblioteca padrao.
"""

import glob
import json
import os
import re
import shutil
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'painel-sessoes'))
import painel  # noqa: E402

ANSI = re.compile(rb'\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Z0-9]|\x1b[=>]')
DIALOGO = ('Quick safety check', 'Do you trust', 'I trust this folder')
# o que o TUI desenha DEPOIS de aceitar: prompt normal e rodape de modo/atalhos
NORMAL = ('Try "', 'for shortcuts', 'shift+tab to cycle', 'accept edits', 'auto mode')
LIMITE_TEXTO = 20_000


def texto_limpo(dados):
    return re.sub(r'\s+', ' ', ANSI.sub(b' ', dados).decode('utf-8', 'replace'))


class Detector:
    """Estado do dialogo: 'aguardando' -> 'dialogo' (visto) -> 'aceita'. 'recusada' vem do fim do processo."""

    def __init__(self):
        self.estado = 'aguardando'
        self.recente = b''         # bytes desenhados desde a ultima entrada do usuario (apos o dialogo aparecer)
        self.resposta_dada = False

    def saida(self, dados):
        """Alimenta com bytes do TUI; devolve o estado."""
        if self.estado == 'aguardando':
            if any(d in texto_limpo(dados) for d in DIALOGO):
                self.estado = 'dialogo'
            return self.estado
        if self.estado == 'dialogo' and self.resposta_dada:
            self.recente = (self.recente + dados)[-LIMITE_TEXTO:]  # bytes: um marcador pode vir partido entre dois pedacos
            if any(m in texto_limpo(self.recente) for m in NORMAL):
                self.estado = 'aceita'
        return self.estado

    def entrada(self, dados):
        """Teclas do usuario. Enter depois do dialogo = resposta dada (aceitar ou recusar); o desfecho vem da tela/saida."""
        if self.estado == 'dialogo' and (b'\r' in dados or b'\n' in dados):
            self.resposta_dada = True
            self.recente = b''


def pasta_do_projeto(pasta, base=None):
    """~/.claude/projects/<caminho com / e . trocados por ->, como o Claude Code nomeia."""
    base = base or os.path.expanduser('~/.claude/projects')
    return os.path.join(base, re.sub(r'[/.]', '-', os.path.realpath(pasta)))


def transcritos(pasta_projeto):
    return set(glob.glob(os.path.join(pasta_projeto, '*.jsonl')))


def tem_prompt_de_usuario(caminho):
    try:
        with open(caminho, errors='replace') as arq:
            for linha in arq:
                if '"user"' not in linha:
                    continue
                try:
                    if painel.prompt_humano(json.loads(linha)):
                        return True
                except ValueError:
                    continue
    except OSError:
        return True  # na duvida, nao apaga
    return False


def limpar_transcritos_vazios(pasta_projeto, antes):
    """Apaga SO os .jsonl criados depois de `antes` (e a pasta <id>/) que nao tem nenhum prompt de usuario."""
    apagados = []
    for caminho in sorted(transcritos(pasta_projeto) - set(antes)):
        if tem_prompt_de_usuario(caminho):
            continue
        os.remove(caminho)
        pasta = caminho[:-6]
        if os.path.isdir(pasta):
            shutil.rmtree(pasta, ignore_errors=True)
        apagados.append(caminho)
    return apagados
