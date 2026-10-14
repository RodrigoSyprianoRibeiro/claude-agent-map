"""Testa as funcoes puras das colunas da doca (bloco <puras> do mapa.html) executando-as no node."""
import json
import os
import shutil
import subprocess
import unittest

AQUI = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def bloco_puras():
    with open(os.path.join(AQUI, 'mapa.html'), encoding='utf-8') as f:
        html = f.read()
    ini = html.index('// <puras>')
    fim = html.index('// </puras>')
    return html[ini:fim]


def rodar(expressao):
    codigo = bloco_puras() + '\nconsole.log(JSON.stringify(' + expressao + '));'
    r = subprocess.run(['node', '-e', codigo], capture_output=True, text=True, timeout=20)
    if r.returncode:
        raise AssertionError(r.stderr)
    return json.loads(r.stdout)


@unittest.skipUnless(shutil.which('node'), 'node ausente')
class TestColunasPuras(unittest.TestCase):
    def test_arrasto_preserva_soma(self):
        e, d = rodar('larguraAposArrasto(600, 600, 100, 320)')
        self.assertEqual((e, d), (700, 500))

    def test_arrasto_respeita_minimo(self):
        self.assertEqual(rodar('larguraAposArrasto(600, 600, 900, 320)'), [880, 320])
        self.assertEqual(rodar('larguraAposArrasto(600, 600, -900, 320)'), [320, 880])

    def test_arrasto_sem_espaco_divide_igual(self):
        self.assertEqual(rodar('larguraAposArrasto(300, 300, 50, 320)'), [300, 300])

    def test_indice_de_solta(self):
        self.assertEqual(rodar('indiceDeSolta([50, 150, 250], 10)'), 0)
        self.assertEqual(rodar('indiceDeSolta([50, 150, 250], 100)'), 1)
        self.assertEqual(rodar('indiceDeSolta([50, 150, 250], 999)'), 3)
        self.assertEqual(rodar('indiceDeSolta([], 5)'), 0)

    def test_lado_da_solta(self):
        self.assertEqual(rodar("ladoDaSolta(100, 0, 400)"), 'esq')
        self.assertEqual(rodar("ladoDaSolta(300, 0, 400)"), 'dir')
        self.assertEqual(rodar("ladoDaSolta(1100, 800, 400)"), 'dir')

    def test_normalizar_larguras(self):
        self.assertEqual(rodar('normalizarLarguras([500, 700], 2)'), [500, 700])
        self.assertEqual(rodar('normalizarLarguras([500, 0], 2)'), [1, 1])
        self.assertEqual(rodar('normalizarLarguras([500], 2)'), [1, 1])
        self.assertEqual(rodar('normalizarLarguras(null, 3)'), [1, 1, 1])


if __name__ == '__main__':
    unittest.main()
