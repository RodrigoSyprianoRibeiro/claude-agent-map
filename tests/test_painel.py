"""Testes da leitura de sessoes (painel.py)."""

import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import painel  # noqa: E402

RAIZ = '/home/x/dev/empresa'


def linha(**campos):
    return json.dumps(campos) + '\n'


def prompt(texto, cwd=RAIZ):
    return linha(type='user', cwd=cwd, message={'role': 'user', 'content': [{'type': 'text', 'text': texto}]})


def ferramenta(nome, entrada, cwd=RAIZ):
    return linha(type='assistant', cwd=cwd, message={
        'stop_reason': 'tool_use', 'content': [{'type': 'tool_use', 'id': 't', 'name': nome, 'input': entrada}]})


class TestTranscrito(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.caminho = os.path.join(self.dir, 'sessao.jsonl')

    def escrever(self, texto, modo='a'):
        with open(self.caminho, modo) as arq:
            arq.write(texto)

    def ler(self, leitura=painel.LEITURA_ABERTA):
        t = painel.Transcrito(self.caminho, leitura)
        t.atualizar()
        return t

    def test_titulo_dado_pelo_usuario_vence_o_da_ia(self):
        self.escrever(linha(type='ai-title', aiTitle='Gerado') + linha(type='custom-title', customTitle='Site SESI'))
        self.assertEqual(self.ler().titulo(), 'Site SESI')

    def test_prompt_ignora_tool_result_e_envelope_de_comando(self):
        self.escrever(prompt('corrigir o login'))
        self.escrever(prompt('<command-name>/model</command-name>'))
        self.escrever(linha(type='user', message={'role': 'user', 'content': [{'type': 'tool_result', 'content': 'x'}]}))
        t = self.ler()
        self.assertEqual(t.primeiro_prompt, 'corrigir o login')
        self.assertEqual(t.ultimo_prompt, 'corrigir o login')

    def test_subprojeto_pelo_cwd_atual(self):
        self.escrever(prompt('oi') + prompt('seguir', cwd=RAIZ + '/sites/wp-content'))
        self.assertEqual(self.ler().subprojeto(RAIZ), 'sites')

    def test_subprojeto_pelos_arquivos_editados_pesa_mais_que_lidos(self):
        self.escrever(prompt('oi'))
        for _ in range(2):
            self.escrever(ferramenta('Read', {'file_path': RAIZ + '/inclutech/a.php'}))
        self.escrever(ferramenta('Edit', {'file_path': RAIZ + '/portal-vagas/b.php'}))
        self.assertEqual(self.ler().subprojeto(RAIZ), 'portal-vagas')

    def test_subprojeto_por_cd_e_git_c_no_bash(self):
        self.escrever(prompt('oi'))
        self.escrever(ferramenta('Bash', {'command': 'cd fabrica && git status'}))
        self.escrever(ferramenta('Bash', {'command': 'git -C %s/fabrica log -1' % RAIZ}))
        self.assertEqual(self.ler().subprojeto(RAIZ), 'fabrica')

    def test_subprojeto_pelo_trabalho_na_vps(self):
        ws = tempfile.mkdtemp()
        os.makedirs(os.path.join(ws, '.claude'))
        with open(os.path.join(ws, '.claude', 'inventario.json'), 'w') as arq:
            json.dump({'hosts': {
                'principal': {'ssh': 'root@203.0.113.5', 'ip': '203.0.113.5',
                              'caminhos': {'laravel': '/var/www/loja', 'crawler': '/var/www/loja-crawler'}},
                'sse': {'ssh': 'root@203.0.113.88', 'ip': '203.0.113.88', 'caminhos': {'sse': '/var/www/loja-sse'}},
                'proxy': {'ssh': 'root@proxy.example.com', 'ip': '198.51.100.182', 'caminhos': {'proxy': '/var/www/loja-proxy'}}}}, arq)
        remotos = [{'pasta': ws, 'inventario': os.path.join(ws, '.claude', 'inventario.json'),
                    'repoDoCaminho': {'laravel': 'loja-app', 'crawler': 'loja-crawler', 'sse': 'loja-sse', 'proxy': 'loja-proxy-wh'}}]
        r = lambda c: [os.path.basename(p) for p in painel.pastas_remotas(c, remotos)]  # noqa: E731
        self.assertEqual(r('ssh root@203.0.113.88 "journalctl -u sse-go -n 50"'), ['loja-sse'])
        self.assertEqual(r('ssh root@proxy.example.com "pm2 logs loja-proxy"'), ['loja-proxy-wh'])
        self.assertEqual(r('ssh root@203.0.113.5 "/var/www/loja-crawler/restart-crawler.sh"'), ['loja-crawler'])
        self.assertEqual(r('ssh root@203.0.113.5 "cd /var/www/loja && php artisan x"'), ['loja-app'])
        self.assertEqual(r('scp a.js root@203.0.113.5:/var/www/loja-crawler/src/'), ['loja-crawler'])
        self.assertEqual(r('ssh root@203.0.113.5 "uptime"'), [])  # principal sem caminho: ambiguo
        self.assertEqual(r('ssh root@203.0.113.50 "uptime"'), [])
        self.assertEqual(r('git status'), [])
        self.assertEqual(painel.pastas_remotas('ssh root@203.0.113.88 "x"'), [])  # sem config: desligado

    def test_arquivo_solto_na_raiz_nao_vira_subprojeto(self):
        self.escrever(prompt('oi') + ferramenta('Read', {'file_path': RAIZ + '/README.md'}))
        self.assertIsNone(self.ler().subprojeto(RAIZ))

    def test_leitura_inicial_pelo_final_descarta_linha_cortada_e_le_a_cabeca(self):
        self.escrever(prompt('primeiro de todos'))
        self.escrever(linha(type='attachment', lixo='x' * 5000))
        self.escrever(linha(type='custom-title', customTitle='No final'))
        t = self.ler(leitura=200)
        self.assertEqual(t.titulo(), 'No final')
        self.assertEqual(t.primeiro_prompt, 'primeiro de todos')

    def test_incremental_com_linha_pela_metade(self):
        completa = linha(type='custom-title', customTitle='Depois')
        self.escrever(completa[:15])
        t = self.ler()
        self.assertIsNone(t.titulo())
        self.escrever(completa[15:])
        t.atualizar()
        self.assertEqual(t.titulo(), 'Depois')

    def test_campos_de_tempo(self):
        def com_hora(texto, hora):
            return json.dumps({'type': 'user', 'cwd': RAIZ, 'timestamp': hora,
                               'message': {'role': 'user', 'content': [{'type': 'text', 'text': texto}]}}) + '\n'

        def resposta(hora, parada, lado=False):
            return json.dumps({'type': 'assistant', 'timestamp': hora, 'isSidechain': lado, 'message': {
                'stop_reason': parada, 'content': [{'type': 'text', 'text': 'ok'}]}}) + '\n'

        self.escrever(com_hora('um', '2026-10-08T10:00:00.000Z'))
        self.escrever(resposta('2026-10-08T10:01:00.000Z', 'end_turn'))
        self.escrever(com_hora('dois', '2026-10-08T11:00:00.000Z'))
        self.escrever(resposta('2026-10-08T11:00:30.000Z', 'tool_use'))
        self.escrever(resposta('2026-10-08T11:05:00.000Z', 'end_turn', lado=True))
        t = self.ler()
        base = painel.epoch('2026-10-08T10:00:00.000Z')
        self.assertEqual(t.inicio_sessao, base)
        self.assertEqual(t.inicio_turno, base + 3600)
        self.assertEqual(t.fim_turno, base + 60)  # sidechain e tool_use nao contam
        self.assertLess(t.fim_turno, t.inicio_turno)  # turno em andamento

    def test_campos_de_tempo_ausentes(self):
        self.escrever(prompt('sem timestamp'))
        t = self.ler()
        self.assertIsNone(t.inicio_sessao)
        self.assertIsNone(t.inicio_turno)
        self.assertIsNone(t.fim_turno)


def usuario(texto, ts, origem=None):
    campos = {'type': 'user', 'timestamp': ts, 'cwd': RAIZ, 'message': {'role': 'user', 'content': [{'type': 'text', 'text': texto}]}}
    if origem:
        campos['origin'] = origem
    return linha(**campos)


def resultado_de_ferramenta(ts, lixo=300):
    return linha(type='user', timestamp=ts, message={'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 't', 'content': 'x' * lixo}]})


class TestInicioDoTurnoEmTranscritoGrande(unittest.TestCase):
    """Cauda sem prompt real do usuario: o turno nao pode herdar o primeiro prompt da cabeca."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.caminho = os.path.join(self.dir, 'sessao.jsonl')
        self.antigos = (painel.BLOCO_BUSCA_PROMPT, painel.MAX_BUSCA_PROMPT)
        painel.BLOCO_BUSCA_PROMPT, painel.MAX_BUSCA_PROMPT = 700, 100_000

    def tearDown(self):
        painel.BLOCO_BUSCA_PROMPT, painel.MAX_BUSCA_PROMPT = self.antigos

    def escrever(self, *linhas):
        with open(self.caminho, 'a') as arq:
            arq.write(''.join(linhas))

    def cauda_sem_prompt(self, n=12):
        return [resultado_de_ferramenta('2026-10-10T12:%02d:00Z' % i) for i in range(n)] + [
            usuario('<task-notification><task-id>x</task-id></task-notification>', '2026-10-10T12:30:00Z', {'kind': 'task-notification'}),
            usuario('mensagem de outro agente via SendMessage', '2026-10-10T12:31:00Z', {'kind': 'peer'}),
        ]

    def ler(self):
        t = painel.Transcrito(self.caminho, 1500)
        t.atualizar()
        t.garantir_inicio_turno()
        return t

    def test_procura_para_tras_o_ultimo_prompt_real_fora_da_cauda(self):
        self.escrever(usuario('primeiro', '2026-10-08T10:00:00Z', {'kind': 'human'}))
        self.escrever(*[resultado_de_ferramenta('2026-10-08T11:%02d:00Z' % i) for i in range(10)])
        self.escrever(usuario('o prompt de verdade', '2026-10-10T11:50:00Z', {'kind': 'human'}))
        self.escrever(*self.cauda_sem_prompt())
        t = self.ler()
        self.assertGreater(t.inicio_leitura, 0)  # a cauda realmente nao cobre o arquivo
        self.assertEqual(t.inicio_turno, painel.epoch('2026-10-10T11:50:00Z'))  # nem o prompt da cabeca, nem inicioSessao
        self.assertEqual(t.inicio_sessao, painel.epoch('2026-10-08T10:00:00Z'))

    def test_prompt_que_cruza_a_fronteira_dos_blocos_de_busca(self):
        self.escrever(usuario('primeiro', '2026-10-08T10:00:00Z', {'kind': 'human'}))
        for tamanho in (50, 120, 333, 480):  # varia o alinhamento do prompt em relacao aos blocos
            self.escrever(*[resultado_de_ferramenta('2026-10-09T11:%02d:00Z' % i, tamanho) for i in range(7)])
        self.escrever(usuario('alvo ' + 'x' * 400, '2026-10-10T11:50:00Z'))
        self.escrever(*self.cauda_sem_prompt(20))
        self.assertEqual(self.ler().inicio_turno, painel.epoch('2026-10-10T11:50:00Z'))

    def test_notificacao_e_mensagem_de_agente_nao_sao_prompt_do_usuario(self):
        self.escrever(usuario('primeiro', '2026-10-08T10:00:00Z', {'kind': 'human'}))
        self.escrever(*self.cauda_sem_prompt(30))
        t = self.ler()
        self.assertEqual(t.inicio_turno, painel.epoch('2026-10-08T10:00:00Z'))  # o unico prompt humano que existe
        self.assertNotEqual(t.ultimo_prompt, 'mensagem de outro agente via SendMessage')

    def test_sem_prompt_na_janela_busca_exposta_como_aproximada(self):
        self.escrever(usuario('primeiro', '2026-10-08T10:00:00Z', {'kind': 'human'}))
        self.escrever(*[resultado_de_ferramenta('2026-10-09T11:%02d:00Z' % (i % 60), 400) for i in range(300)])
        self.escrever(*self.cauda_sem_prompt(3))
        painel.MAX_BUSCA_PROMPT = 2000  # nao chega ate o prompt da cabeca
        t = self.ler()
        self.assertIsNone(t.inicio_turno)
        self.assertIsNotNone(t.primeiro_ts_cauda)
        self.assertGreater(t.primeiro_ts_cauda, painel.epoch('2026-10-09T00:00:00Z'))

    def test_montar_expoe_inicio_aproximado_e_nunca_o_da_sessao(self):
        base = tempfile.mkdtemp()
        os.makedirs(os.path.join(base, 'projects', 'x'))
        sid = 'sess-grande'
        self.caminho = os.path.join(base, 'projects', 'x', sid + '.jsonl')
        self.escrever(usuario('primeiro', '2026-10-08T10:00:00Z', {'kind': 'human'}))
        self.escrever(*[resultado_de_ferramenta('2026-10-09T11:%02d:00Z' % (i % 60), 400) for i in range(300)])
        self.escrever(*self.cauda_sem_prompt(3))
        painel.MAX_BUSCA_PROMPT = 2000
        antigo = painel.LEITURA_ABERTA
        painel.LEITURA_ABERTA = 1500
        try:
            p = painel.Painel(base, 14)
            p.caminhos[sid] = self.caminho
            s = p._montar(sid, {'cwd': RAIZ, 'status': 'busy'}, time.time())
        finally:
            painel.LEITURA_ABERTA = antigo
        self.assertIsNone(s['inicioTurno'])
        self.assertIsNotNone(s['inicioTurnoAproximado'])
        self.assertEqual(s['inicioSessao'], painel.epoch('2026-10-08T10:00:00Z'))
        self.assertGreater(s['inicioTurnoAproximado'], s['inicioSessao'])


class TestPainel(unittest.TestCase):
    def setUp(self):
        self.base = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.base, 'state', 'atencao'))
        self.p = painel.Painel(self.base, 14)

    def alerta(self, sid, ts, tipo='permission_prompt'):
        with open(os.path.join(self.base, 'state', 'atencao', sid + '.json'), 'w') as arq:
            json.dump({'ts': ts, 'tipo': tipo, 'mensagem': 'Claude needs your permission'}, arq)

    def test_alerta_vale_ate_o_transcrito_ser_escrito_depois(self):
        t = painel.Transcrito('/nao/existe')
        t.mtime = time.time()
        self.alerta('s1', t.mtime - 1)
        self.assertIsNotNone(self.p.atencao('s1', t))
        t.mtime += 60  # tool aprovada escreveu no transcrito
        self.assertIsNone(self.p.atencao('s1', t))

    def test_processo_vivo_confere_o_inicio(self):
        with open('/proc/self/stat') as arq:
            inicio = arq.read().rsplit(')', 1)[1].split()[19]
        self.assertTrue(painel.processo_vivo(os.getpid(), inicio))
        self.assertFalse(painel.processo_vivo(os.getpid(), '1'))  # PID reaproveitado
        self.assertFalse(painel.processo_vivo(999999999, None))


class TestNovaSessao(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.projeto = os.path.join(self.home, 'dev', 'empresa')
        os.makedirs(self.projeto)

    def test_monta_argv_com_nome_e_prompt_depois_de_duplo_traco(self):
        argv, pasta = painel.validar_nova_sessao(
            {'cwd': self.projeto, 'prompt': '--dangerously-skip-permissions', 'nome': ' rodar  testes '}, self.home)
        self.assertEqual(argv, ['claude', '--bg', '--name', 'rodar testes', '--', '--dangerously-skip-permissions'])
        self.assertEqual(pasta, os.path.realpath(self.projeto))

    def test_recusa_pasta_fora_da_home_inexistente_ou_prompt_vazio(self):
        casos = [{'cwd': '/tmp', 'prompt': 'x'},
                 {'cwd': self.home + '-vizinha', 'prompt': 'x'},
                 {'cwd': os.path.join(self.projeto, '..', '..', '..'), 'prompt': 'x'},
                 {'cwd': os.path.join(self.home, 'nao-existe'), 'prompt': 'x'},
                 {'cwd': self.projeto, 'prompt': '   '},
                 'nao e objeto']
        for dados in casos:
            with self.assertRaises(ValueError, msg=dados):
                painel.validar_nova_sessao(dados, self.home)

    def test_origem(self):
        self.assertTrue(painel.origem_permitida(None, 8766))
        self.assertTrue(painel.origem_permitida('http://127.0.0.1:8766', 8766))
        self.assertFalse(painel.origem_permitida('https://evil.com', 8766))
        self.assertFalse(painel.origem_permitida('null', 8766))


if __name__ == '__main__':
    unittest.main()
