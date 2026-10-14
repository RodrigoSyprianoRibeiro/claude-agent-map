"""Testes do config.json (workspaces, validacao e padroes)."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402

HOME = '/home/x'


class TestConfig(unittest.TestCase):
    def test_padrao_sem_workspaces(self):
        c = config.Config(home=HOME)
        self.assertEqual(c.pasta_projetos, HOME + '/dev')
        self.assertEqual(c.workspaces, [])
        self.assertEqual(c.remotos, [])

    def test_til_expande_pela_home_do_config(self):
        c = config.Config({'pastaProjetos': '~/codigo', 'workspaces': [{'id': 'emp', 'pasta': '~/codigo/emp'}]}, HOME)
        self.assertEqual(c.pasta_projetos, HOME + '/codigo')
        self.assertEqual(c.workspaces[0].pasta, HOME + '/codigo/emp')

    def test_nome_e_cor_padrao(self):
        c = config.Config({'workspaces': [{'id': 'a', 'pasta': '~/dev/a'}, {'id': 'b', 'pasta': '~/dev/b', 'nome': 'Bê', 'cor': '#AABBCC'}]}, HOME)
        self.assertEqual([w.publico() for w in c.workspaces],
                         [{'id': 'a', 'nome': 'a', 'cor': config.PALETA[0]}, {'id': 'b', 'nome': 'Bê', 'cor': '#aabbcc'}])

    def test_validacao(self):
        ruins = [
            {'workspaces': [{'id': 'A B', 'pasta': '~/dev/a'}]},              # id invalido
            {'workspaces': [{'id': 'home', 'pasta': '~/dev/a'}]},             # id reservado
            {'workspaces': [{'id': 'a'}]},                                    # sem pasta
            {'workspaces': [{'id': 'a', 'pasta': '/etc'}]},                   # fora da home
            {'workspaces': [{'id': 'a', 'pasta': '~/dev/a', 'cor': 'red'}]},  # cor invalida
            {'workspaces': [{'id': 'a', 'pasta': '~/dev/a'}, {'id': 'a', 'pasta': '~/dev/b'}]},  # id repetido
            {'workspaces': [{'id': 'a', 'pasta': '~/dev/a', 'irmaos': '../*'}]},  # padrao com barra
            {'workspaces': [{'id': 'a', 'pasta': '~/dev/a', 'remoto': {'inventario': 'x'}}]},  # sem repoDoCaminho
            {'workspaces': 'a'},
            [],
        ]
        for dados in ruins:
            with self.assertRaises(ValueError, msg=dados):
                config.Config(dados, HOME)

    def test_workspace_mais_especifico_e_irmaos(self):
        c = config.Config({'workspaces': [
            {'id': 'loja', 'pasta': '~/dev/loja-workspace', 'irmaos': 'loja-*'},
            {'id': 'interno', 'pasta': '~/dev/loja-workspace/interno'},
        ]}, HOME)
        self.assertEqual(c.workspace_da_pasta(HOME + '/dev/loja-workspace/app').id, 'loja')
        self.assertEqual(c.workspace_da_pasta(HOME + '/dev/loja-workspace/interno/x').id, 'interno')
        self.assertIsNone(c.workspace_da_pasta(HOME + '/dev/loja-workspacex'))
        loja = c.por_id['loja']
        self.assertTrue(loja.e_irmao('loja-wt'))
        self.assertFalse(loja.e_irmao('loja-workspace'))  # a propria pasta do workspace
        self.assertFalse(loja.e_irmao('outro'))

    def test_nos_fixos_ignoram_worktree_e_pasta_sem_git(self):
        home = tempfile.mkdtemp()
        base = os.path.join(home, 'dev', 'loja-workspace')
        os.makedirs(os.path.join(base, 'loja-sse', '.git'))
        os.makedirs(os.path.join(base, 'loja-sse-wt'))
        open(os.path.join(base, 'loja-sse-wt', '.git'), 'w').close()
        os.makedirs(os.path.join(base, 'analytics', '.git'))
        os.makedirs(os.path.join(base, 'loja-solta'))
        c = config.Config({'workspaces': [{'id': 'loja', 'pasta': '~/dev/loja-workspace', 'nosFixos': 'loja-*'}]}, home)
        self.assertEqual(c.workspaces[0].fixos(), ['loja-sse'])

    def test_remoto(self):
        c = config.Config({'workspaces': [{'id': 'loja', 'pasta': '~/dev/loja', 'remoto': {'repoDoCaminho': {'laravel': 'loja-app'}}}]}, HOME)
        self.assertEqual(c.remotos, [{'pasta': HOME + '/dev/loja', 'inventario': HOME + '/dev/loja/.claude/inventario.json',
                                      'repoDoCaminho': {'laravel': 'loja-app'}}])

    def test_carregar(self):
        pasta = tempfile.mkdtemp()
        self.assertEqual(config.carregar(os.path.join(pasta, 'nao-existe.json'), HOME).workspaces, [])
        ruim = os.path.join(pasta, 'ruim.json')
        with open(ruim, 'w') as arq:
            arq.write('{ nao e json')
        with self.assertRaises(ValueError):
            config.carregar(ruim, HOME)
        bom = os.path.join(pasta, 'bom.json')
        with open(bom, 'w') as arq:
            json.dump({'workspaces': [{'id': 'a', 'pasta': '~/dev/a'}]}, arq)
        self.assertEqual(config.carregar(bom, HOME).workspaces[0].id, 'a')

    def test_exemplo_do_repositorio_e_valido(self):
        exemplo = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'config.example.json')
        c = config.carregar(exemplo, HOME)
        self.assertGreaterEqual(len(c.workspaces), 1)


if __name__ == '__main__':
    unittest.main()


class TestChavesEmIngles(unittest.TestCase):
    def test_ingles_e_portugues_dao_o_mesmo_resultado(self):
        en = config.Config({'projectsDir': '~/code', 'workspaces': [{
            'id': 'shop', 'name': 'Shop', 'path': '~/code/shop', 'color': '#112233', 'aliases': {'app': ['store']},
            'siblings': 'shop-*', 'pinned': 'shop-*', 'remote': {'inventory': 'inv.json', 'repoByPath': {'web': 'shop-web'}}}]}, HOME)
        pt = config.Config({'pastaProjetos': '~/code', 'workspaces': [{
            'id': 'shop', 'nome': 'Shop', 'pasta': '~/code/shop', 'cor': '#112233', 'apelidos': {'app': ['store']},
            'irmaos': 'shop-*', 'nosFixos': 'shop-*', 'remoto': {'inventario': 'inv.json', 'repoDoCaminho': {'web': 'shop-web'}}}]}, HOME)
        a, b = en.workspaces[0], pt.workspaces[0]
        self.assertEqual(en.pasta_projetos, pt.pasta_projetos)
        self.assertEqual((a.nome, a.pasta, a.cor, a.apelidos, a.irmaos, a.nos_fixos, a.remoto),
                         (b.nome, b.pasta, b.cor, b.apelidos, b.irmaos, b.nos_fixos, b.remoto))

    def test_as_duas_formas_juntas_e_erro(self):
        with self.assertRaises(ValueError):
            config.Config({'workspaces': [{'id': 'a', 'path': '~/dev/a', 'pasta': '~/dev/b'}]}, HOME)
