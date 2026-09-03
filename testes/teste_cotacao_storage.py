"""Teste do transporte FTPS da cotacao, com um servidor FTPS falso em memoria.

Por que existe: o FTPS foi escrito em 03/09/2026 depois de descobrir no painel da Umbler
que a hospedagem nao oferece SFTP (SSH so para git). Esse caminho nao pode ser testado
contra o servidor real sem um deploy, e um erro nele custa um ciclo inteiro de
push + espera de deploy. Aqui ele roda em milissegundos.

Cobre:
  - leitura de data do fato `modify` do MLSD, que vem em UTC
  - fallback para NLST + MDTM quando o servidor nao suporta MLSD
  - subir(): grava .part e renomeia, e apaga o destino antigo se existir
  - procurar(): casa prefixo, escolhe o mais novo, marca vencido pela validade
  - apagar_antigos(): apaga o que passou do prazo e preserva o resto
"""

import os
import sys
import time
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["BRIDGE_TOKEN"] = "token-de-teste"
os.environ["STORAGE_BACKEND"] = "ftps"
os.environ["COTACAO_BASE_DIR"] = "/public/cotacao"
os.environ["COTACAO_VALIDADE_HORAS"] = "24"

import cotacao_api as api  # noqa: E402
from ftplib import error_perm  # noqa: E402

falhas = []


def checar(nome, condicao, detalhe=""):
    print(("  ok  " if condicao else " FALHA ") + nome + (f"  {detalhe}" if detalhe else ""))
    if not condicao:
        falhas.append(nome)


def carimbo(epoch: float) -> str:
    """epoch -> texto do fato `modify` do MLSD (UTC, sem fuso no texto)."""
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y%m%d%H%M%S")


class FTPSFalso:
    """O minimo da interface de ftplib que o modulo usa."""

    def __init__(self, arquivos=None, suporta_mlsd=True):
        self.arquivos = dict(arquivos or {})  # nome -> (bytes, epoch)
        self.suporta_mlsd = suporta_mlsd
        self.pasta = "/"
        self.pastas_criadas = []
        self.fechado = False
        self.pasv = None
        self.prot_p_chamado = False

    # --- navegacao
    def cwd(self, caminho):
        if caminho == "/":
            self.pasta = "/"
            return
        if caminho in ("public", "cotacao"):
            self.pasta = self.pasta.rstrip("/") + "/" + caminho
            return
        raise error_perm(f"550 {caminho}: sem essa pasta")

    def mkd(self, nome):
        self.pastas_criadas.append(nome)

    # --- listagem
    def mlsd(self, caminho=".", facts=None):
        if not self.suporta_mlsd:
            raise error_perm("500 MLSD nao suportado")
        for nome, (_, quando) in self.arquivos.items():
            yield nome, {"type": "file", "modify": carimbo(quando)}

    def nlst(self, *args):
        return list(self.arquivos)

    def voidcmd(self, comando):
        if comando.startswith("MDTM "):
            nome = comando[5:]
            return "213 " + carimbo(self.arquivos[nome][1])
        return "200 ok"

    # --- transferencia
    def storbinary(self, comando, fluxo):
        nome = comando.split(" ", 1)[1]
        self.arquivos[nome] = (fluxo.read(), time.time())

    def size(self, nome):
        return len(self.arquivos[nome][0])

    def delete(self, nome):
        if nome not in self.arquivos:
            raise error_perm(f"550 {nome}: sem esse arquivo")
        del self.arquivos[nome]

    def rename(self, de, para):
        self.arquivos[para] = self.arquivos.pop(de)

    def getwelcome(self):
        return "220 servidor falso"

    def quit(self):
        self.fechado = True

    def close(self):
        self.fechado = True


def instalar(ftps):
    api._ftps = lambda: ftps
    return ftps


print("== backend ativo ==")
checar("modulo em ftps", api.BACKEND == "ftps", api.BACKEND)

print("\n== leitura da data do MLSD ==")
agora = time.time()
checar("ida e volta do carimbo",
       abs(api._epoch_de_modify(carimbo(agora)) - agora) < 1.5,
       f"{api._epoch_de_modify(carimbo(agora)):.0f} vs {agora:.0f}")
checar("carimbo do MLSD e lido como UTC, nao como hora local",
       api._epoch_de_modify("19700101000000") == 0.0,
       str(api._epoch_de_modify("19700101000000")))

print("\n== subir ==")
srv = instalar(FTPSFalso())
url = api.subir(b"%PDF-falso", "WA-9217-tokenaleatorio1.pdf")
checar("URL montada com a base publica",
       url == f"{api.PUBLIC_BASE_URL}/WA-9217-tokenaleatorio1.pdf", url)
checar("arquivo final existe", "WA-9217-tokenaleatorio1.pdf" in srv.arquivos)
checar("nao sobrou .part", not any(n.endswith(".part") for n in srv.arquivos),
       str(list(srv.arquivos)))
checar("conexao encerrada", srv.fechado)

print("\n== subir por cima de arquivo existente ==")
srv = instalar(FTPSFalso({"WA-1-t.pdf": (b"velho", time.time() - 60)}))
api.subir(b"%PDF-novo", "WA-1-t.pdf")
checar("substituiu o conteudo", srv.arquivos["WA-1-t.pdf"][0] == b"%PDF-novo")

print("\n== procurar ==")
hora = 3600
srv = instalar(FTPSFalso({
    "WA-9217-antigo.pdf": (b"a", time.time() - 30 * hora),
    "WA-9217-novo.pdf": (b"b", time.time() - 2 * hora),
    "WA-0001-outro.pdf": (b"c", time.time() - 1 * hora),
    "COT-0309-XXXX-z.pdf": (b"d", time.time() - 1 * hora),
    "lixo.txt": (b"e", time.time()),
}))
achado = api.procurar("WA-9217")
checar("escolheu o mais novo entre dois do mesmo numero",
       achado["nome"] == "WA-9217-novo.pdf", achado["nome"])
checar("idade calculada", 1.9 < achado["idade_horas"] < 2.1, str(achado["idade_horas"]))
checar("dentro da validade nao expira", achado["expirado"] is False)
checar("nao vazou arquivo de outro numero", "WA-0001" not in achado["nome"])

achado = api.procurar("WA-0001")
checar("prefixo exato, nao parcial", achado["nome"] == "WA-0001-outro.pdf", achado["nome"])
checar("numero inexistente devolve None", api.procurar("WA-7777") is None)

srv = instalar(FTPSFalso({"WA-5-t.pdf": (b"a", time.time() - 25 * hora)}))
achado = api.procurar("WA-5")
checar("25h marca expirado", achado["expirado"] is True, str(achado["idade_horas"]))

print("\n== fallback quando o servidor nao suporta MLSD ==")
srv = instalar(FTPSFalso({"WA-9-t.pdf": (b"a", time.time() - 3 * hora)}, suporta_mlsd=False))
achado = api.procurar("WA-9")
checar("achou por NLST + MDTM", achado is not None and achado["nome"] == "WA-9-t.pdf")
checar("data veio do MDTM", 2.9 < achado["idade_horas"] < 3.1, str(achado["idade_horas"]))

print("\n== pasta que nao existe ==")
class SemPasta(FTPSFalso):
    def cwd(self, caminho):
        if caminho == "/":
            return
        raise error_perm("550 sem essa pasta")

instalar(SemPasta())
checar("pasta ausente devolve None em vez de estourar", api.procurar("WA-1") is None)

print("\n== apagar_antigos ==")
dia = 86400
srv = instalar(FTPSFalso({
    "velho1.pdf": (b"a", time.time() - 40 * dia),
    "velho2.pdf": (b"b", time.time() - 31 * dia),
    "novo.pdf": (b"c", time.time() - 2 * dia),
}))
apagados = api.apagar_antigos(30)
checar("apagou os dois vencidos", apagados == 2, str(apagados))
checar("preservou o recente", list(srv.arquivos) == ["novo.pdf"], str(list(srv.arquivos)))

srv = instalar(FTPSFalso({"novo.pdf": (b"c", time.time())}))
checar("nada vencido, nada apagado", api.apagar_antigos(30) == 0)

print("\n== ponta a ponta: gerar, achar, reaproveitar ==")
srv = instalar(FTPSFalso())
AUTH = "Bearer token-de-teste"
corpo = api.CotacaoRequest(
    numero="WA-4242",
    cliente="CLIENTE DE TESTE LTDA",
    telefone="21900000000",
    itens=[{"codigo": "1", "descricao": "ITEM DE TESTE", "quantidade": 2,
            "unitario": 10.0, "disponivel": 2}],
)
r1 = api.criar_cotacao(corpo, AUTH)
checar("gerou e subiu de verdade pelo FTPS falso",
       r1.pdf_url.endswith(".pdf") and len(srv.arquivos) == 1, str(list(srv.arquivos)))
checar("PDF de verdade no servidor",
       list(srv.arquivos.values())[0][0][:4] == b"%PDF")
r2 = api.criar_cotacao(corpo, AUTH)
checar("segunda chamada reaproveita", r2.reaproveitado is True and r2.pdf_url == r1.pdf_url)
checar("nao criou arquivo novo", len(srv.arquivos) == 1, str(len(srv.arquivos)))
b = api.buscar_cotacao("WA-4242", AUTH)
checar("busca pelo numero acha", b.pdf_url == r1.pdf_url)

print()
if falhas:
    print(f"FALHOU: {len(falhas)} -> {falhas}")
    raise SystemExit(1)
print("TUDO OK")
