"""Anexos de imagem do composer: gravados em disco e referenciados so por uuid.

O cliente nunca conhece nem manda caminho: o servidor resolve o uuid para o arquivo. Pasta 0700,
arquivos 0600, imagens com mais de 7 dias sao apagadas na subida do servidor.
So biblioteca padrao.
"""

import os
import re
import time
import uuid

TIPOS = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp', 'image/gif': 'gif'}
MAX_BYTES = 15 * 1024 * 1024
DIAS_VALIDADE = 7
ID_ANEXO = re.compile(r'^[0-9a-f]{32}$')


def pasta_padrao():
    return os.path.expanduser('~/.cache/mapa-agentes/anexos')


def confere_assinatura(tipo, inicio):
    """O Content-Type declarado tem que bater com os primeiros bytes do arquivo."""
    if tipo == 'image/png':
        return inicio.startswith(b'\x89PNG\r\n\x1a\n')
    if tipo == 'image/jpeg':
        return inicio.startswith(b'\xff\xd8\xff')
    if tipo == 'image/webp':
        return inicio[:4] == b'RIFF' and inicio[8:12] == b'WEBP'
    if tipo == 'image/gif':
        return inicio[:6] in (b'GIF87a', b'GIF89a')
    return False


class Anexos:
    def __init__(self, pasta=None):
        self.pasta = pasta or pasta_padrao()

    def _garantir_pasta(self):
        os.makedirs(self.pasta, mode=0o700, exist_ok=True)
        os.chmod(self.pasta, 0o700)

    def salvar(self, tipo, dados):
        """Grava e devolve o uuid. Levanta ValueError (tipo, tamanho ou conteudo invalido)."""
        if tipo not in TIPOS:
            raise ValueError('tipo nao permitido (use png, jpeg, webp ou gif)')
        if not dados or len(dados) > MAX_BYTES:
            raise ValueError('arquivo vazio ou maior que 15 MB')
        if not confere_assinatura(tipo, dados[:16]):
            raise ValueError('o conteudo nao e %s' % tipo)
        self._garantir_pasta()
        anexo_id = uuid.uuid4().hex
        caminho = os.path.join(self.pasta, '%s.%s' % (anexo_id, TIPOS[tipo]))
        fd = os.open(caminho, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'wb') as arq:
            arq.write(dados)
        return anexo_id

    def caminho(self, anexo_id):
        """Arquivo do uuid, ou None se o id for invalido ou nao existir."""
        if not isinstance(anexo_id, str) or not ID_ANEXO.match(anexo_id):
            return None
        for ext in set(TIPOS.values()):
            c = os.path.join(self.pasta, '%s.%s' % (anexo_id, ext))
            if os.path.isfile(c):
                return c
        return None

    def limpar_antigos(self, agora=None):
        """Apaga anexos com mais de 7 dias. Devolve quantos."""
        agora = agora or time.time()
        n = 0
        try:
            nomes = os.listdir(self.pasta)
        except OSError:
            return 0
        for nome in nomes:
            c = os.path.join(self.pasta, nome)
            try:
                if os.path.isfile(c) and agora - os.path.getmtime(c) > DIAS_VALIDADE * 86400:
                    os.remove(c)
                    n += 1
            except OSError:
                pass
        return n
