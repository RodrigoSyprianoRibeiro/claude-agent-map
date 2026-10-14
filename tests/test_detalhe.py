"""Testes do detalhe da sessao: python3 -m unittest test_detalhe.py"""

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import detalhe  # noqa: E402

SID = '11111111-2222-3333-4444-555555555555'
USO = {'input_tokens': 1000, 'output_tokens': 200, 'cache_read_input_tokens': 0, 'cache_creation_input_tokens': 0}


def msg(mid, ts, modelo='claude-sonnet-5-5', parada=None, usage=None, conteudo=None):
    return json.dumps({'type': 'assistant', 'timestamp': ts, 'message': {
        'id': mid, 'model': modelo, 'stop_reason': parada, 'usage': usage or USO,
        'content': conteudo or [{'type': 'text', 'text': 'ok'}]}}) + '\n'


def uso_ferramenta(tid, nome, entrada):
    return [{'type': 'tool_use', 'id': tid, 'name': nome, 'input': entrada}]


class Base(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        self.proj = os.path.join(self.base, 'projects', '-home-x')
        os.makedirs(os.path.join(self.proj, SID, 'subagents'))
        self.principal = os.path.join(self.proj, SID + '.jsonl')
        self.d = detalhe.Detalhes(self.base)

    def escrever(self, caminho, texto):
        with open(caminho, 'a') as arq:
            arq.write(texto)

    def sub(self, nome, linhas, meta=None, idade=0):
        c = os.path.join(self.proj, SID, 'subagents', 'agent-%s.jsonl' % nome)
        self.escrever(c, linhas)
        with open(c[:-6] + '.meta.json', 'w') as arq:
            json.dump(meta or {'agentType': 'Explore', 'description': 'varrer'}, arq)
        if idade:
            os.utime(c, (time.time() - idade, time.time() - idade))
        return c


class TestDedup(Base):
    def test_ultima_linha_de_cada_id_vale(self):
        parcial = dict(USO, output_tokens=5)
        final = dict(USO, output_tokens=2000)
        self.escrever(self.principal, msg('m1', '2026-10-10T10:00:00Z', usage=parcial) + msg('m1', '2026-10-10T10:00:01Z', usage=final, parada='end_turn'))
        r = self.d.sessao(SID)
        so = detalhe.custo_usage(final, 'claude-sonnet-5-5')
        self.assertAlmostEqual(r['custoPorModelo'][0]['custo'], so)
        self.assertEqual(r['custoPorModelo'][0]['chamadas'], 1)
        self.assertEqual(r['custoPorModelo'][0]['modelo'], 'claude-sonnet-5-5')

    def test_leitura_incremental_e_cache(self):
        self.escrever(self.principal, msg('m1', '2026-10-10T10:00:00Z'))
        self.assertEqual(self.d.sessao(SID)['custoPorModelo'][0]['chamadas'], 1)
        self.escrever(self.principal, msg('m2', '2026-10-10T10:01:00Z'))
        self.assertEqual(self.d.sessao(SID)['custoPorModelo'][0]['chamadas'], 2)
        self.assertEqual(len(self.d.cache), 1)

    def test_id_invalido_ou_inexistente(self):
        self.assertIsNone(self.d.sessao('../../etc/passwd'))
        self.assertIsNone(self.d.sessao('99999999-0000-0000-0000-000000000000'))

    def test_final_de_arquivo_grande_marca_parcial(self):
        self.escrever(self.principal, msg('m0', '2026-10-10T09:00:00Z') + 'x' * 600 + '\n')
        self.escrever(self.principal, msg('m1', '2026-10-10T10:00:00Z'))
        antigo = detalhe.LEITURA_PRINCIPAL
        detalhe.LEITURA_PRINCIPAL = len(msg('m1', '2026-10-10T10:00:00Z')) + 40
        try:
            r = self.d.sessao(SID)
        finally:
            detalhe.LEITURA_PRINCIPAL = antigo
        self.assertTrue(r['parcial'])
        self.assertEqual(r['custoPorModelo'][0]['chamadas'], 1)


class TestAdvisor(Base):
    def test_consultas_ao_advisor_com_tokens_e_custo(self):
        uso = {'input_tokens': 10, 'output_tokens': 20, 'iterations': [
            {'type': 'message', 'model': 'claude-sonnet-5-5', 'input_tokens': 10, 'output_tokens': 20},
            {'type': 'advisor_message', 'model': 'claude-opus-5-5', 'input_tokens': 50_000, 'cache_read_input_tokens': 30_000, 'output_tokens': 1_000}]}
        self.escrever(self.principal, msg('a1', '2026-10-10T10:00:00Z', usage=uso, parada='end_turn'))
        r = self.d.sessao(SID)
        self.assertEqual(len(r['advisor']['consultas']), 1)
        c = r['advisor']['consultas'][0]
        self.assertEqual((c['modelo'], c['tokensLidos'], c['tokensSaida']), ('claude-opus-5-5', 80_000, 1000))
        self.assertGreater(c['custo'], 0)
        self.assertAlmostEqual(r['advisor']['custo'], c['custo'])
        modelos = {m['modelo'] for m in r['custoPorModelo']}
        self.assertEqual(modelos, {'claude-sonnet-5-5', 'claude-opus-5-5'})

    def test_sem_advisor(self):
        self.escrever(self.principal, msg('m1', '2026-10-10T10:00:00Z'))
        self.assertEqual(self.d.sessao(SID)['advisor'], {'consultas': [], 'custo': 0})


class TestSubagentes(Base):
    def test_status_duracao_modelo_e_ferramentas(self):
        self.escrever(self.principal, msg('p1', '2026-10-10T10:00:00Z'))
        self.sub('concluido',
                 msg('s1', '2026-10-10T10:00:10Z', modelo='claude-haiku-5-5', conteudo=uso_ferramenta('t1', 'Bash', {'description': 'listar'}))
                 + msg('s2', '2026-10-10T10:02:40Z', modelo='claude-haiku-5-5', parada='end_turn'), idade=600)
        self.sub('rodando', msg('s3', '2026-10-10T10:05:00Z', parada='tool_use', conteudo=uso_ferramenta('t2', 'Read', {'file_path': '/a/b/arq.py'})),
                 meta={'agentType': 'general-purpose', 'description': 'implementar'})
        self.sub('parado', msg('s4', '2026-10-10T10:06:00Z', parada='tool_use'), idade=600)
        r = {s['id']: s for s in self.d.sessao(SID)['subagentes']}
        self.assertEqual(r['agent-concluido']['status'], 'concluido')
        self.assertEqual(r['agent-concluido']['duracao'], 150)
        self.assertEqual(r['agent-concluido']['modelo'], 'claude-haiku-5-5')
        self.assertEqual(r['agent-concluido']['ferramentas'], 1)
        self.assertEqual(r['agent-concluido']['ultima']['resumo'], 'listar')
        self.assertEqual(r['agent-concluido']['tipo'], 'Explore')
        self.assertGreater(r['agent-concluido']['custo'], 0)
        self.assertEqual(r['agent-rodando']['status'], 'rodando')
        self.assertEqual(r['agent-rodando']['tipo'], 'general-purpose')
        self.assertEqual(r['agent-rodando']['ultima']['ferramenta'], 'Read')
        self.assertEqual(r['agent-rodando']['ultima']['resumo'], 'arq.py')
        self.assertEqual(r['agent-parado']['status'], 'parado')

    def test_sem_meta_vira_subagente_generico(self):
        c = os.path.join(self.proj, SID, 'subagents', 'agent-x.jsonl')
        self.escrever(c, msg('s1', '2026-10-10T10:00:00Z', parada='end_turn'))
        self.escrever(self.principal, msg('p1', '2026-10-10T10:00:00Z'))
        self.assertEqual(self.d.sessao(SID)['subagentes'][0]['tipo'], 'subagente')

    def test_custo_da_sessao_soma_principal_e_subagentes(self):
        self.escrever(self.principal, msg('p1', '2026-10-10T10:00:00Z', modelo='claude-opus-5-5'))
        self.sub('a', msg('s1', '2026-10-10T10:00:10Z', modelo='claude-haiku-5-5', parada='end_turn'))
        self.sub('b', msg('s2', '2026-10-10T10:00:10Z', modelo='claude-haiku-5-5', parada='end_turn'))
        por = {m['modelo']: m for m in self.d.sessao(SID)['custoPorModelo']}
        self.assertEqual(por['claude-haiku-5-5']['chamadas'], 2)
        self.assertEqual(por['claude-opus-5-5']['chamadas'], 1)


class TestLinhaDoTempo(Base):
    def test_ultimas_40_ferramentas_em_ordem(self):
        linhas = ''
        for i in range(55):
            linhas += msg('m%d' % i, '2026-10-10T10:%02d:00Z' % i, conteudo=uso_ferramenta('t%d' % i, 'Bash', {'command': 'echo %d' % i}))
        self.escrever(self.principal, linhas)
        f = self.d.sessao(SID)['ferramentas']
        self.assertEqual(len(f), 40)
        self.assertEqual(f[-1]['resumo'], 'echo 54')
        self.assertEqual(f[0]['resumo'], 'echo 15')

    def test_mcp_vira_servidor_acao(self):
        self.escrever(self.principal, msg('m1', '2026-10-10T10:00:00Z', conteudo=uso_ferramenta('t1', 'mcp__slack__slack_post_message', {})))
        self.assertEqual(self.d.sessao(SID)['ferramentas'][0]['ferramenta'], 'slack:slack_post_message')


if __name__ == '__main__':
    unittest.main()
