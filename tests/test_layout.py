"""Testes do layout (regioes, nos e posicoes)."""

import math
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import config  # noqa: E402
import layout  # noqa: E402

HOME = '/home/x'
AGORA = time.time()
SEMPRE = lambda _caminho: True  # noqa: E731
# dois workspaces: um com subprojetos simples e um com repos irmaos (worktrees) e nos fixos
CONFIG = config.Config({'workspaces': [
    {'id': 'loja', 'nome': 'Loja', 'pasta': HOME + '/dev/loja-workspace', 'irmaos': 'loja-*', 'nosFixos': 'loja-*'},
    {'id': 'empresa', 'nome': 'Empresa', 'pasta': HOME + '/dev/empresa'},
]}, HOME)


def sessao(raiz, sub=None, aberta=True, idade=0, sid=None):
    return {'id': sid or raiz + str(sub), 'aberta': aberta, 'raiz': raiz, 'cwd': raiz,
            'projeto': os.path.basename(raiz), 'subprojeto': sub, 'ultimaAtividade': AGORA - idade}


class TestClassificar(unittest.TestCase):
    def test_regioes(self):
        c = lambda *a: layout.classificar(sessao(*a), HOME, SEMPRE, CONFIG)  # noqa: E731
        self.assertEqual(c(HOME + '/dev/loja-workspace', 'loja-sse'), ('loja', 'loja-sse'))
        self.assertEqual(c(HOME + '/dev/loja-workspace/loja-app'), ('loja', 'loja-app'))
        self.assertEqual(c(HOME + '/dev/loja-wt'), ('loja', 'loja-wt'))
        self.assertEqual(c(HOME + '/dev/empresa', 'portal-vagas'), ('empresa', 'portal-vagas'))
        self.assertEqual(c(HOME + '/dev/empresa/sites/wp'), ('empresa', 'sites'))
        self.assertEqual(c(HOME + '/dev/editor'), ('editor', 'geral'))

    def test_no_geral(self):
        self.assertEqual(layout.classificar(sessao(HOME + '/dev/loja-workspace'), HOME, SEMPRE, CONFIG), ('loja', 'geral'))
        self.assertEqual(layout.classificar(sessao(HOME + '/dev/empresa'), HOME, SEMPRE, CONFIG), ('empresa', 'geral'))

    def test_subprojeto_so_vale_se_for_pasta_abaixo_da_raiz(self):
        raiz = HOME + '/dev/empresa'
        so_sites = lambda c: c == raiz + '/sites'  # noqa: E731
        self.assertEqual(layout.classificar(sessao(raiz, 'sites'), HOME, so_sites), ('empresa', 'sites'))
        self.assertEqual(layout.classificar(sessao(raiz, '%s'), HOME, so_sites), ('empresa', 'geral'))
        self.assertEqual(layout.classificar(sessao(raiz, 'memory'), HOME, so_sites), ('empresa', 'geral'))

    def test_claude_e_fora_da_home_nunca_viram_no(self):
        self.assertIsNone(layout.classificar(sessao(HOME + '/.claude/projects/x/memory'), HOME, SEMPRE))
        self.assertIsNone(layout.classificar(sessao(HOME + '/.claude'), HOME, SEMPRE))
        self.assertIsNone(layout.classificar(sessao('/tmp/claude-1000/x/scratchpad'), HOME, SEMPRE))

    def test_raiz_igual_a_home_vira_no_home(self):
        self.assertEqual(layout.classificar(sessao(HOME, 'dev'), HOME, SEMPRE, CONFIG), ('home', 'geral'))

    def test_worktree_dentro_de_projeto_usa_o_projeto(self):
        raiz = HOME + '/dev/editor/.claude/worktrees/feat-x/backend'
        self.assertEqual(layout.classificar(sessao(raiz), HOME, SEMPRE, CONFIG), ('editor', 'geral'))

    def test_montar_ignora_sessoes_sem_no(self):
        l = layout.Layout(os.path.join(tempfile.mkdtemp(), 'p.json'), HOME, SEMPRE, CONFIG)
        r = l.montar([sessao(HOME + '/.claude/projects/x'), sessao(HOME, None)], AGORA)
        self.assertEqual([n['id'] for n in r['nos']], ['home/geral'])
        self.assertEqual(len(r['sessoes']), 1)


class TestFoco(unittest.TestCase):
    def test_parse_foco(self):
        self.assertEqual(layout.parse_foco(''), (None, None))
        self.assertEqual(layout.parse_foco(None), (None, None))
        self.assertEqual(layout.parse_foco('empresa'), ('empresa', None))
        self.assertEqual(layout.parse_foco('empresa/portal-vagas'), ('empresa', 'empresa/portal-vagas'))
        self.assertEqual(layout.parse_foco('empresa/'), ('empresa', None))
        self.assertEqual(layout.parse_foco('editor'), ('editor', None))
        self.assertEqual(layout.parse_foco('editor/frontend'), ('editor', 'editor/frontend'))
        for ruim in ('/abc', '.claude', 'a?b', 'a&b'):
            with self.assertRaises(ValueError, msg=ruim):
                layout.parse_foco(ruim)


class TestLayout(unittest.TestCase):
    def setUp(self):
        self.arq = os.path.join(tempfile.mkdtemp(), 'sub', 'posicoes.json')
        self.l = layout.Layout(self.arq, HOME, SEMPRE, CONFIG)

    def nos(self, resultado):
        """Posicao de cada no RELATIVA ao centro da regiao (e o que fica guardado e estavel)."""
        c = {g['id']: (g['x'], g['y']) for g in resultado['regioes']}
        return {n['id']: (round(n['x'] - c[n['regiao']][0], 1), round(n['y'] - c[n['regiao']][1], 1)) for n in resultado['nos']}

    def test_estabilidade_ao_acrescentar_no(self):
        base = [sessao(HOME + '/dev/empresa', 'b'), sessao(HOME + '/dev/empresa', 'c')]
        antes = self.nos(self.l.montar(base, AGORA))
        depois = self.nos(self.l.montar(base + [sessao(HOME + '/dev/empresa', 'a')], AGORA))
        for chave, pos in antes.items():
            self.assertEqual(depois[chave], pos)
        self.assertEqual(len(set(depois.values())), 3)  # slots distintos
        # e persiste entre instancias
        novo = layout.Layout(self.arq, HOME, SEMPRE, CONFIG)
        self.assertEqual(self.nos(novo.montar(base, AGORA)), {k: depois[k] for k in antes})

    def test_geral_no_centro_da_regiao(self):
        r = self.l.montar([sessao(HOME + '/dev/empresa', 'a'), sessao(HOME + '/dev/empresa')], AGORA)
        n = self.nos(r)
        self.assertEqual(n['empresa/geral'], (0, 0))

    def test_posicao_salva_tem_prioridade(self):
        s = [sessao(HOME + '/dev/empresa', 'a')]
        regiao = {g['id']: g for g in self.l.montar(s, AGORA)['regioes']}['empresa']
        self.assertTrue(self.l.definir_posicao('empresa/a', regiao['x'] + 123.4, regiao['y'] - 50))
        self.assertEqual(self.nos(self.l.montar(s, AGORA))['empresa/a'], (123.4, -50))
        # slot do no arrastado continua reservado
        outro = self.nos(self.l.montar(s + [sessao(HOME + '/dev/empresa', 'b')], AGORA))['empresa/b']
        self.assertNotEqual(outro, layout.posicao_do_slot('empresa', 0))
        self.assertEqual(layout.Layout(self.arq, HOME, SEMPRE, CONFIG).posicoes['empresa/a']['x'], 123.4)

    def test_posicao_invalida_ou_no_desconhecido(self):
        self.l.montar([sessao(HOME + '/dev/empresa', 'a')], AGORA)
        self.assertFalse(self.l.definir_posicao('empresa/zzz', 1, 1))
        self.assertFalse(self.l.definir_posicao('empresa/a', float('nan'), 1))
        self.assertFalse(self.l.definir_posicao('empresa/a', 'x', 1))
        self.assertFalse(self.l.definir_posicao('empresa/a', 10 ** 9, 1))

    def test_no_so_com_fechadas_antigas_nao_aparece(self):
        velha = sessao(HOME + '/dev/editor', aberta=False, idade=15 * 86400)
        recente = sessao(HOME + '/dev/financas', aberta=False, idade=2 * 86400)
        r = self.l.montar([velha, recente], AGORA)
        self.assertEqual([n['id'] for n in r['nos']], ['financas/geral'])
        self.assertEqual(len(r['sessoes']), 1)
        self.assertEqual(r['sessoes'][0]['no'], 'financas/geral')

    def test_distancia_minima_entre_slots(self):
        for regiao in ('loja', 'empresa'):
            pts = [layout.posicao_do_slot(regiao, k) for k in range(60)]
            for i in range(len(pts)):
                for j in range(i + 1, len(pts)):
                    self.assertGreaterEqual(math.dist(pts[i], pts[j]), 300, (regiao, i, j))

    def test_regiao_cresce_com_o_numero_de_nos(self):
        poucas = self.l.montar([sessao(HOME + '/dev/empresa', 'a')], AGORA)['regioes']
        muitas = self.l.montar([sessao(HOME + '/dev/empresa', str(i)) for i in range(20)], AGORA)['regioes']
        raio = lambda r: next(g['raio'] for g in r if g['id'] == 'empresa')  # noqa: E731
        self.assertGreater(raio(muitas), raio(poucas))

    def test_regioes_sem_sobreposicao_e_com_folga_fixa(self):
        nos = [sessao(HOME + '/dev/empresa', str(i)) for i in range(24)] + [sessao(HOME + '/dev/loja-workspace', 'r%d' % i) for i in range(8)]
        nos += [sessao(HOME + '/dev/p%d' % i) for i in range(12)]
        r = self.l.montar(nos, AGORA)['regioes']
        self.assertEqual(len(r), 14)
        for i in range(len(r)):
            for j in range(i + 1, len(r)):
                d = math.dist((r[i]['x'], r[i]['y']), (r[j]['x'], r[j]['y']))
                self.assertGreaterEqual(d, r[i]['raio'] + r[j]['raio'] + layout.FOLGA_REGIOES / 2 - 0.5, (r[i]['id'], r[j]['id']))

    def test_grandes_lado_a_lado_e_avulsos_abaixo(self):
        r = {g['id']: g for g in self.l.montar([sessao(HOME + '/dev/empresa', 'a'), sessao(HOME + '/dev/loja-workspace', 'b'),
                                                sessao(HOME + '/dev/editor')], AGORA)['regioes']}
        self.assertLess(r['loja']['x'], r['empresa']['x'])
        self.assertGreater(r['editor']['y'] - r['editor']['raio'], r['empresa']['y'] + r['empresa']['raio'] - 1)
        self.assertEqual([g for g in r], ['loja', 'empresa', 'editor'])

    def test_avulso_sem_contorno_e_com_nome_do_projeto(self):
        r = self.l.montar([sessao(HOME + '/dev/editor')], AGORA)
        reg = next(g for g in r['regioes'] if g['id'] == 'editor')
        self.assertFalse(reg['contorno'])
        self.assertEqual(r['nos'][0]['id'], 'editor/geral')
        self.assertEqual(r['nos'][0]['nome'], 'editor')

    def test_no_solitario_em_subpasta_mostra_projeto_e_pasta(self):
        r = self.l.montar([sessao(HOME + '/dev/agencia/prospeccao')], AGORA)
        self.assertEqual((r['nos'][0]['id'], r['nos'][0]['nome']), ('agencia/prospeccao', 'agencia / prospeccao'))

    def test_avulso_com_subpastas_reais_vira_regiao_com_varios_nos(self):
        r = self.l.montar([sessao(HOME + '/dev/editor', 'frontend'), sessao(HOME + '/dev/editor', 'backend'),
                           sessao(HOME + '/dev/editor')], AGORA)
        self.assertEqual(sorted(n['id'] for n in r['nos']), ['editor/backend', 'editor/frontend', 'editor/geral'])
        reg = next(g for g in r['regioes'] if g['id'] == 'editor')
        self.assertTrue(reg['contorno'])

    def test_avulso_novo_nao_move_os_existentes(self):
        base = [sessao(HOME + '/dev/zeta'), sessao(HOME + '/dev/beta')]
        antes = {n['id']: (n['x'], n['y']) for n in self.l.montar(base, AGORA)['nos']}
        depois = {n['id']: (n['x'], n['y']) for n in self.l.montar(base + [sessao(HOME + '/dev/alfa')], AGORA)['nos']}
        for k, v in antes.items():
            self.assertEqual(depois[k], v)  # alfa entra no fim da fila, nao no comeco
        self.assertEqual(len(set(depois.values())), 3)
        # persistido: outra instancia reproduz a mesma ordem
        novo = layout.Layout(self.arq, HOME, SEMPRE, CONFIG)
        again = {n['id']: (n['x'], n['y']) for n in novo.montar(base + [sessao(HOME + '/dev/alfa')], AGORA)['nos']}
        self.assertEqual(again, depois)

    def test_avulso_antigo_some_da_tela(self):
        velho = sessao(HOME + '/dev/editor', aberta=False, idade=20 * 86400)
        self.assertEqual(self.l.montar([velho], AGORA)['regioes'][2:], [])

    def test_repos_fixos_aparecem_sem_sessao_e_contam_as_antigas(self):
        l = layout.Layout(self.arq, HOME, SEMPRE, CONFIG, fixos=lambda: [('loja', 'loja-sse'), ('loja', 'loja-crawler')])
        velha = sessao(HOME + '/dev/loja-workspace/loja-crawler', aberta=False, idade=20 * 86400)
        r = l.montar([velha], AGORA)
        nos = {n['id']: n for n in r['nos']}
        self.assertEqual(sorted(nos), ['loja/loja-crawler', 'loja/loja-sse'])
        self.assertEqual((nos['loja/loja-sse']['total'], nos['loja/loja-crawler']['total']), (0, 1))

    def test_posicoes_antigas_de_outros_sao_descartadas(self):
        import json
        os.makedirs(os.path.dirname(self.arq))
        with open(self.arq, 'w') as arq:
            json.dump({'outros/x': {'regiao': 'outros', 'slot': 0, 'x': 0, 'y': 0}, 'empresa/a': {'regiao': 'empresa', 'slot': 0, 'x': 0, 'y': 0}}, arq)
        l = layout.Layout(self.arq, HOME, SEMPRE, CONFIG)
        self.assertEqual(list(l.posicoes), ['empresa/a'])

    def test_pasta_do_no(self):
        existe = lambda c: c in (HOME + '/dev/empresa', HOME + '/dev/empresa/portal-vagas', HOME + '/dev/editor', HOME + '/dev/editor/frontend', HOME)  # noqa: E731
        self.assertEqual(layout.pasta_do_no(HOME, 'empresa', 'geral', existe, CONFIG), HOME + '/dev/empresa')
        self.assertEqual(layout.pasta_do_no(HOME, 'empresa', 'portal-vagas', existe, CONFIG), HOME + '/dev/empresa/portal-vagas')
        self.assertEqual(layout.pasta_do_no(HOME, 'editor', 'frontend', existe, CONFIG), HOME + '/dev/editor/frontend')
        self.assertEqual(layout.pasta_do_no(HOME, 'editor', 'geral', existe, CONFIG), HOME + '/dev/editor')
        self.assertEqual(layout.pasta_do_no(HOME, 'home', 'geral', existe, CONFIG), HOME)
        self.assertIsNone(layout.pasta_do_no(HOME, 'empresa', 'fantasma', existe, CONFIG))


if __name__ == '__main__':
    unittest.main()


class TestSemWorkspaces(unittest.TestCase):
    """Sem config.json: nenhuma regiao grande, cada projeto e uma regiao avulsa."""

    def test_projetos_viram_regioes_avulsas(self):
        self.assertEqual(layout.classificar(sessao(HOME + '/dev/empresa', 'portal'), HOME, SEMPRE), ('empresa', 'portal'))
        l = layout.Layout(os.path.join(tempfile.mkdtemp(), 'p.json'), HOME, SEMPRE)
        r = l.montar([sessao(HOME + '/dev/a'), sessao(HOME + '/dev/b'), sessao(HOME + '/dev/c', 'x')], AGORA)
        self.assertFalse(any(g['grande'] for g in r['regioes']))
        self.assertEqual(sorted(g['id'] for g in r['regioes']), ['a', 'b', 'c'])

    def test_tres_workspaces_em_fila_na_ordem_do_config(self):
        cfg = config.Config({'workspaces': [{'id': w, 'pasta': HOME + '/dev/' + w} for w in ('z', 'a', 'm')]}, HOME)
        l = layout.Layout(os.path.join(tempfile.mkdtemp(), 'p.json'), HOME, SEMPRE, cfg)
        r = l.montar([sessao(HOME + '/dev/a', 'x')], AGORA)['regioes']
        self.assertEqual([g['id'] for g in r], ['z', 'a', 'm'])
        self.assertTrue(r[0]['x'] < r[1]['x'] < r[2]['x'])
        self.assertEqual({g['nome'] for g in r}, {'Z', 'A', 'M'})


class TestProjetoPeloTexto(unittest.TestCase):
    SUB = ['loja-virtual', 'portal-vagas', 'horas', 'atualizacoes-laravel', 'pedidos', 'sala', 'sd']

    def p(self, *textos, apelidos=None):
        return layout.projeto_pelo_texto(textos, self.SUB, apelidos)

    def test_nome_da_pasta_no_titulo_ou_no_prompt(self):
        self.assertEqual(self.p('portal-vagas-projeto'), 'portal-vagas')
        self.assertEqual(self.p(None, 'olha o board do Portal Vagas pra mim'), 'portal-vagas')
        self.assertEqual(self.p('Atualizações do Laravel e dos pedidos', None), None)  # duas pastas citadas: ambiguo
        self.assertEqual(self.p('atualizações do laravel'), 'atualizacoes-laravel')
        self.assertEqual(self.p('relatório da SALA'), 'sala')

    def test_erro_de_digitacao_em_nome_longo(self):
        self.assertEqual(self.p('lojavirtal-cliente-atualizacoes'), 'loja-virtual')
        self.assertIsNone(self.p('busque atualizações do projeto'))  # "atualizacoes" sozinho nao basta
        self.assertIsNone(self.p('sla'))  # nome curto: so exato

    def test_titulo_vale_antes_do_prompt(self):
        self.assertEqual(self.p('horas: apontamento', 'e depois os pedidos'), 'horas')

    def test_apelidos(self):
        apelidos = {'loja-virtual': ['Acme', 'Acme.io'], 'inexistente': ['x']}
        self.assertEqual(self.p(None, 'busque atualizações da Acme', apelidos=apelidos), 'loja-virtual')
        self.assertEqual(self.p('acmeio-revisao', apelidos=apelidos), 'loja-virtual')

    def test_layout_usa_o_texto_quando_nao_ha_arquivo(self):
        cfg = config.Config({'workspaces': [{'id': 'empresa', 'pasta': HOME + '/dev/empresa', 'apelidos': {'loja-virtual': ['acme']}}]}, HOME)
        listadas = []
        listar = lambda raiz: listadas.append(raiz) or ['loja-virtual', 'portal-vagas']  # noqa: E731
        l = layout.Layout(os.path.join(tempfile.mkdtemp(), 'p.json'), HOME, SEMPRE, cfg, listar=listar)
        s1 = dict(sessao(HOME + '/dev/empresa'), titulo='acme-relatorio')
        s2 = dict(sessao(HOME + '/dev/empresa', sid='b'), primeiroPrompt='status do portal vagas')
        s3 = dict(sessao(HOME + '/dev/empresa', sid='c'), titulo='conversa solta')
        r = l.montar([s1, s2, s3], AGORA)
        self.assertEqual(sorted(x['no'] for x in r['sessoes']), ['empresa/geral', 'empresa/loja-virtual', 'empresa/portal-vagas'])
        l.montar([s1, s2, s3], AGORA)
        self.assertEqual(listadas, [HOME + '/dev/empresa'])  # listagem e inferencia em cache

    def test_apelidos_invalidos(self):
        with self.assertRaises(ValueError):
            config.Config({'workspaces': [{'id': 'a', 'pasta': HOME + '/dev/a', 'apelidos': {'x': 'acme'}}]}, HOME)
