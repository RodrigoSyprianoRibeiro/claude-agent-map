"""Testes da leitura da conversa (conversa.py): tipos de linha, blocos da mesma mensagem, leitura incremental."""

import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import conversa  # noqa: E402


def linha(**campos):
    return json.dumps(campos) + '\n'


def assistente(uuid, msg_id, bloco):
    return linha(type='assistant', uuid=uuid, timestamp='2026-10-11T12:00:00.000Z',
                 message={'id': msg_id, 'model': 'claude-opus-5-5', 'role': 'assistant', 'content': [bloco]})


def usuario(uuid, conteudo, **extra):
    return linha(type='user', uuid=uuid, timestamp='2026-10-11T12:00:00.000Z', message={'role': 'user', 'content': conteudo}, **extra)


class Base(unittest.TestCase):
    def setUp(self):
        fd, self.caminho = tempfile.mkstemp(suffix='.jsonl')
        os.close(fd)

    def tearDown(self):
        os.unlink(self.caminho)

    def gravar(self, *linhas, modo='w'):
        with open(self.caminho, modo, encoding='utf-8') as f:
            f.write(''.join(linhas))


class TestItens(Base):
    def test_blocos_da_mesma_mensagem_viram_itens_separados(self):
        self.gravar(
            assistente('u1', 'msg_1', {'type': 'thinking', 'thinking': '', 'signature': 'SEGREDO'}),
            assistente('u2', 'msg_1', {'type': 'text', 'text': 'Vou ler o arquivo.'}),
            assistente('u3', 'msg_1', {'type': 'tool_use', 'id': 'toolu_1', 'name': 'Read', 'input': {'file_path': '/a/b.py'}}),
        )
        r = conversa.ler(self.caminho)
        self.assertEqual([i['t'] for i in r['itens']], ['pensou', 'texto', 'ferramenta'])
        self.assertEqual([i['u'] for i in r['itens']], ['u1', 'u2', 'u3'])
        self.assertNotIn('SEGREDO', json.dumps(r))
        self.assertEqual(r['itens'][2]['resumo'], 'b.py')

    def test_tool_result_nao_vira_mensagem_do_usuario(self):
        self.gravar(usuario('u1', [{'type': 'tool_result', 'tool_use_id': 'toolu_1', 'content': 'saida', 'is_error': True}],
                            toolUseResult={'x': 1}))
        [item] = conversa.ler(self.caminho)['itens']
        self.assertEqual((item['t'], item['id'], item['texto'], item['erro']), ('resultado', 'toolu_1', 'saida', True))

    def test_saida_longa_e_cortada(self):
        self.gravar(usuario('u1', [{'type': 'tool_result', 'tool_use_id': 't', 'content': [{'type': 'text', 'text': 'x' * 10000}]}]))
        [item] = conversa.ler(self.caminho)['itens']
        self.assertEqual(len(item['texto']), conversa.MAX_SAIDA)
        self.assertEqual(item['cortado'], 10000 - conversa.MAX_SAIDA)

    def test_prompt_humano_meta_envelope_e_notificacao(self):
        self.gravar(
            usuario('u1', 'oi, tudo bem?'),
            usuario('u2', '<system-reminder>lembrete</system-reminder>', isMeta=True),
            usuario('u3', '<command-name>/model</command-name>\n<command-args>opus</command-args>'),
            usuario('u4', '<local-command-stdout>Set model to \x1b[1mOpus\x1b[22m</local-command-stdout>'),
            usuario('u5', '<task-notification><summary>Comando terminou</summary></task-notification>',
                    origin={'kind': 'task-notification'}),
            usuario('u6', [{'type': 'text', 'text': '[Request interrupted by user]'}]),
            usuario('u7', '<local-command-caveat>ignorar</local-command-caveat>'),
        )
        itens = conversa.ler(self.caminho)['itens']
        self.assertEqual([(i['t'], i.get('tipo'), i['texto']) for i in itens], [
            ('usuario', None, 'oi, tudo bem?'),
            ('aviso', 'comando', '/model opus'),
            ('aviso', 'saida', 'Set model to Opus'),
            ('aviso', 'notificacao', 'Comando terminou'),
            ('aviso', 'interrompido', 'Interrompido'),
        ])

    def test_imagem_do_usuario_conta_sem_mandar_o_conteudo(self):
        self.gravar(usuario('u1', [{'type': 'image', 'source': {'data': 'BASE64GRANDE'}}, {'type': 'text', 'text': 'olha isso'}]))
        [item] = conversa.ler(self.caminho)['itens']
        self.assertEqual((item['texto'], item['imagens']), ('olha isso', 1))
        self.assertNotIn('BASE64GRANDE', json.dumps(item))

    def test_ignora_sidechain_e_tipos_de_controle(self):
        self.gravar(
            linha(type='ai-title', aiTitle='x'), linha(type='permission-mode', permissionMode='auto'),
            usuario('u1', 'do subagente', isSidechain=True),
            linha(type='system', subtype='compact_boundary', uuid='u2'),
        )
        itens = conversa.ler(self.caminho)['itens']
        self.assertEqual([(i['t'], i['tipo']) for i in itens], [('aviso', 'compactado')])


class TestLeitura(Base):
    def test_incremental_e_linha_incompleta_fica_para_depois(self):
        self.gravar(usuario('u1', 'um'))
        r = conversa.ler(self.caminho)
        self.assertEqual(len(r['itens']), 1)
        parcial = usuario('u2', 'dois')
        self.gravar(parcial[:20], modo='a')
        r2 = conversa.ler(self.caminho, desde=r['cursor'])
        self.assertEqual((r2['itens'], r2['cursor']), ([], r['cursor']))
        self.gravar(parcial[20:], modo='a')
        r3 = conversa.ler(self.caminho, desde=r2['cursor'])
        self.assertEqual([i['texto'] for i in r3['itens']], ['dois'])
        self.assertEqual(r3['cursor'], os.path.getsize(self.caminho))

    def test_final_comeca_em_linha_inteira_e_anteriores_completam(self):
        linhas = [usuario('u%d' % i, 'mensagem %d %s' % (i, 'x' * 50)) for i in range(40)]
        self.gravar(*linhas)
        velho = conversa.JANELA
        conversa.JANELA = 1000  # janela menor que o arquivo: corta no meio de uma linha
        try:
            r = conversa.ler(self.caminho)
            self.assertGreater(r['inicio'], 0)
            textos = [i['texto'] for i in r['itens']]
            self.assertTrue(textos[-1].startswith('mensagem 39'))
            vistos = list(textos)
            ate = r['inicio']
            while ate:
                antes = conversa.ler(self.caminho, ate=ate)
                vistos = [i['texto'] for i in antes['itens']] + vistos
                self.assertLess(antes['inicio'], ate)
                ate = antes['inicio']
        finally:
            conversa.JANELA = velho
        self.assertEqual([t.split()[1] for t in vistos], [str(i) for i in range(40)])

    def test_arquivo_encolheu_pede_reinicio(self):
        self.gravar(usuario('u1', 'um'))
        r = conversa.ler(self.caminho, desde=10_000)
        self.assertTrue(r['reiniciar'])

    def test_limite_por_leitura_avisa_que_ha_mais(self):
        self.gravar(*[usuario('u%d' % i, 'm' * 200) for i in range(20)])
        velho = conversa.MAX_INCREMENTO
        conversa.MAX_INCREMENTO = 1000
        try:
            r = conversa.ler(self.caminho, desde=0)
            self.assertTrue(r['mais'])
            total, cursor = len(r['itens']), r['cursor']
            while True:
                r = conversa.ler(self.caminho, desde=cursor)
                total += len(r['itens'])
                cursor = r['cursor']
                if not r['mais']:
                    break
        finally:
            conversa.MAX_INCREMENTO = velho
        self.assertEqual(total, 20)


if __name__ == '__main__':
    unittest.main()
