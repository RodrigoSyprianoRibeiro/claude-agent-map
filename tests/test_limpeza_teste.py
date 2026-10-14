import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import limpeza_teste as lt  # noqa: E402

SID = 'aaaaaaaa-1111-2222-3333-444444444444'
OUTRO = 'bbbbbbbb-1111-2222-3333-444444444444'


class TestLimpeza(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.proj = os.path.join(self.base, '-home-x')
        os.makedirs(self.proj)

    def criar(self, sid, titulo):
        with open(os.path.join(self.proj, sid + '.jsonl'), 'w') as f:
            f.write(json.dumps({'type': 'user'}) + '\n')
            if titulo:
                f.write(json.dumps({'type': 'custom-title', 'customTitle': titulo}) + '\n')
        os.makedirs(os.path.join(self.proj, sid, 'subagents'))

    def test_apaga_so_o_id_pedido_e_so_se_for_teste(self):
        self.criar(SID, 'teste-uso')
        self.criar(OUTRO, 'teste-outro')
        self.assertTrue(lt.apagar_transcrito_de_teste(SID, self.base))
        self.assertFalse(os.path.exists(os.path.join(self.proj, SID + '.jsonl')))
        self.assertFalse(os.path.exists(os.path.join(self.proj, SID)))
        self.assertTrue(os.path.exists(os.path.join(self.proj, OUTRO + '.jsonl')))  # nao e o id pedido

    def test_recusa_sessao_sem_titulo_de_teste(self):
        self.criar(SID, 'Visual desenvolvimento')
        self.assertFalse(lt.apagar_transcrito_de_teste(SID, self.base))
        self.criar(OUTRO, None)
        self.assertFalse(lt.apagar_transcrito_de_teste(OUTRO, self.base))
        self.assertTrue(os.path.exists(os.path.join(self.proj, SID + '.jsonl')))
        self.assertTrue(os.path.exists(os.path.join(self.proj, OUTRO + '.jsonl')))

    def test_id_malformado(self):
        for ruim in ('../x', '*', '', None, 'a/b' * 5):
            self.assertFalse(lt.apagar_transcrito_de_teste(ruim, self.base))


if __name__ == '__main__':
    unittest.main()
