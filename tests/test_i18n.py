"""Confere o dicionario EN do mapa.html (bloco <i18n>): toda chamada tr('...') tem traducao, os parametros
{0}, {1} batem, o HTML estatico e as mensagens de erro do servidor estao cobertos."""
import json
import os
import re
import shutil
import subprocess
import unittest
from html.parser import HTMLParser

AQUI = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ler(nome):
    with open(os.path.join(AQUI, nome), encoding='utf-8') as f:
        return f.read()


HTML = ler('mapa.html')
# literal JS entre aspas simples (com escapes) logo apos tr(
LITERAL_TR = re.compile(r"\btr\(\s*('(?:[^'\\\n]|\\.)*')")
# tr(cond ? 'a' : 'b', ...): os dois ramos
TERNARIO_TR = re.compile(r"\btr\([^'()]*\?\s*('(?:[^'\\\n]|\\.)*')\s*:\s*('(?:[^'\\\n]|\\.)*')")


def node(codigo):
    r = subprocess.run(['node', '-e', codigo], capture_output=True, text=True, timeout=20)
    if r.returncode:
        raise AssertionError(r.stderr)
    return json.loads(r.stdout)


def dicionario():
    ini, fim = HTML.index('// <i18n>'), HTML.index('// </i18n>')
    return node(HTML[ini:fim] + '\nconsole.log(JSON.stringify(EN));')


def literais_tr():
    fontes = LITERAL_TR.findall(HTML) + [x for par in TERNARIO_TR.findall(HTML) for x in par]
    return node('console.log(JSON.stringify([' + ','.join(fontes) + ']));')


class TextosEstaticos(HTMLParser):
    """Nos de texto e atributos traduziveis do <body>, fora de <script>/<style>."""

    def __init__(self):
        super().__init__()
        self.no_body = False
        self.ignorar = 0
        self.textos = []

    def handle_starttag(self, tag, attrs):
        if tag == 'body':
            self.no_body = True
        if tag in ('script', 'style'):
            self.ignorar += 1
        if self.no_body:
            for k, v in attrs:
                if k in ('title', 'placeholder', 'aria-label') and v:
                    self.textos.append(v)

    def handle_endtag(self, tag):
        if tag in ('script', 'style'):
            self.ignorar -= 1

    def handle_data(self, data):
        if self.no_body and not self.ignorar and data.strip():
            self.textos.append(data.strip())


# texto estatico que e igual nos dois idiomas (simbolos, nomes de modelo, niveis de esforco)
SEM_TRADUCAO = {'●', '◐', '○', '0', '--:--:--', '▾', '▸', '+', 'Opus 5.5', 'Sonnet 5.5', 'Haiku 5.5', 'Fable 5.1',
                'low', 'medium', 'high', 'xhigh', 'max', '‹', '›'}
PLACEHOLDER = re.compile(r'\{\d+\}')


@unittest.skipUnless(shutil.which('node'), 'node ausente')
class TestI18n(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.en = dicionario()

    def test_todo_tr_tem_traducao(self):
        faltam = sorted({t for t in literais_tr() if t not in self.en})
        self.assertEqual(faltam, [])

    def test_nomes_do_i18n_nao_sao_redeclarados(self):
        # uma variavel local chamada tr esconderia a funcao (ex.: a barra de custo usava "const tr")
        for nome in ('tr', 'trServidor', 'LANG', 'LOCALE', 'EN', 'EN_SERVIDOR', 'nomeExibido'):
            decl = re.findall(r'\b(?:const|let|var|function)\s+' + nome + r'\b', HTML)
            self.assertLessEqual(len(decl), 1, nome)

    def test_parametros_batem(self):
        for pt, en in self.en.items():
            self.assertEqual(sorted(PLACEHOLDER.findall(pt)), sorted(PLACEHOLDER.findall(en)), pt)

    def test_ingles_sem_acento_nem_travessao(self):
        for pt, en in self.en.items():
            self.assertNotRegex(en, r'[À-ÿ—]', pt)

    def test_html_estatico_coberto(self):
        p = TextosEstaticos()
        p.feed(HTML)
        faltam = sorted({t for t in p.textos if t not in SEM_TRADUCAO and t not in self.en})
        self.assertEqual(faltam, [])

    def test_mensagens_do_servidor_cobertas(self):
        msgs = set()
        for nome in ('servidor.py', 'painel.py', 'anexos.py'):
            fonte = ler(nome)
            msgs |= set(re.findall(r"\berro\(\d+, '([^'%]*)'\)", fonte))
            msgs |= set(re.findall(r"\bValueError\('([^'%]*)'\)", fonte))
            msgs |= set(re.findall(r"'erro': '([^'%]*)'", fonte))
            msgs |= set(re.findall(r"'mensagem': '([^'%]*)'", fonte))
            msgs |= set(re.findall(r"^MSG_[A-Z_]+ = '([^'%]*)'", fonte, re.M))
        self.assertTrue(msgs)
        self.assertEqual(sorted(m for m in msgs if m not in self.en), [])


if __name__ == '__main__':
    unittest.main()
