# -*- coding: utf-8 -*-
"""
bridge_danfe.py — VTEX -> DANFE PDF -> URL
==========================================

Serviço HTTP para o caso PASSIVO: o cliente pede a nota no WhatsApp, o agente
chama esta bridge com o orderId da VTEX, e recebe de volta a URL do PDF.

    Code Action (Weni)  --POST {orderId}-->  bridge
                                              |-- GET pedido na VTEX
                                              |-- extrai embeddedInvoice (XML)
                                              |-- valida (cStat/tpAmb)
                                              |-- gera DANFE em PDF
                                              |-- sobe no storage
                        <--{pdf_url}--------  |

NÃO usa WinThor. O XML autorizado vem do próprio pedido da VTEX, no campo
packageAttachment.packages[].embeddedInvoice — confirmado com pedido real.
Isso dispensa VPN, exposição do ERP e credenciais do WinThor.

Rodar:
    pip install fastapi uvicorn brazilfiscalreport paramiko  # boto3 se usar s3
    uvicorn bridge_danfe:app --host 0.0.0.0 --port 8080

Variáveis de ambiente (nada de credencial no código):
    BRIDGE_TOKEN          token que o Code Action deve enviar em Authorization
    VTEX_ACCOUNT          nome da conta VTEX (ex.: bisturi)
    VTEX_ENVIRONMENT      default vtexcommercestable
    VTEX_APP_KEY          X-VTEX-API-AppKey
    VTEX_APP_TOKEN        X-VTEX-API-AppToken
    STORAGE_BACKEND       "sftp" / "ftps" (hospedagem Umbler) ou "s3" (R2/S3)
    -- se sftp:  SFTP_HOST, SFTP_PORT, SFTP_USER, SFTP_PASSWORD (ou SFTP_KEY_PATH),
                 SFTP_BASE_DIR, PUBLIC_BASE_URL
    -- se ftps:  SFTP_HOST, FTPS_PORT (default 21), SFTP_USER, SFTP_PASSWORD,
                 SFTP_BASE_DIR, PUBLIC_BASE_URL
    -- se s3:    R2_ACCOUNT_ID, R2_BUCKET, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY,
                 URL_EXPIRA_SEGUNDOS (default 604800 = 7 dias)

STATUS DOS TESTES
  TESTADO ✅  extração do embeddedInvoice, validação, geração do PDF e o
              tratamento de múltiplas notas por pedido (ver test_bridge.py)
  NÃO TESTADO ⚠️  chamada real à VTEX e upload real no R2 (precisam credenciais)
"""

import os
import re
import xml.etree.ElementTree as ET
from typing import List, Optional

import requests
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

NS = {"nfe": "http://www.portalfiscal.inf.br/nfe"}
STATUS_AUTORIZADOS = {"100", "150"}
TIMEOUT = 30

app = FastAPI(title="Bridge DANFE", version="1.0")


# ---------------------------------------------------------------- modelos
class PedidoRequest(BaseModel):
    orderId: str
    # opcional: se o cliente citou uma nota específica, filtra por ela
    invoiceNumber: Optional[str] = None


class NotaResponse(BaseModel):
    numero: Optional[str]
    serie: Optional[str]
    chave: str
    pdf_url: str


class RespostaOk(BaseModel):
    orderId: str
    notas: List[NotaResponse]


# ---------------------------------------------------------------- VTEX
def buscar_pedido_vtex(order_id: str) -> dict:
    account = os.environ["VTEX_ACCOUNT"]
    env = os.environ.get("VTEX_ENVIRONMENT", "vtexcommercestable")
    resp = requests.get(
        f"https://{account}.{env}.com.br/api/oms/pvt/orders/{order_id}",
        headers={
            "X-VTEX-API-AppKey": os.environ["VTEX_APP_KEY"],
            "X-VTEX-API-AppToken": os.environ["VTEX_APP_TOKEN"],
            "Accept": "application/json",
        },
        timeout=TIMEOUT,
    )
    if resp.status_code == 404:
        raise HTTPException(404, f"Pedido {order_id} não encontrado na VTEX.")
    if resp.status_code != 200:
        raise HTTPException(502, f"VTEX retornou {resp.status_code}.")
    return resp.json()


def extrair_xmls(pedido: dict, invoice_number: Optional[str] = None) -> List[dict]:
    """
    Extrai os XMLs de nota do pedido VTEX.

    Um pedido pode ter VÁRIAS notas (entrega parcial), então retorna lista.
    Ignora pacotes sem embeddedInvoice — ex.: nota registrada só com a chave,
    sem o arquivo, ou nota de devolução sem XML anexado.

    TESTADO ✅ com estrutura de pedido real e com casos de borda.
    """
    pacotes = ((pedido.get("packageAttachment") or {}).get("packages")) or []
    achados = []
    for p in pacotes:
        xml = p.get("embeddedInvoice")
        if not xml:
            continue
        num = str(p.get("invoiceNumber") or "").strip()
        if invoice_number and num != str(invoice_number).strip():
            continue
        achados.append({"xml": xml, "invoiceNumber": num})
    return achados


# ---------------------------------------------------------------- validação
def validar_xml(xml_content: str) -> dict:
    """
    Confere que a NF-e está autorizada e é de produção ANTES de gerar o PDF.
    Evita entregar ao cliente DANFE de nota rejeitada ou de homologação (esta
    sairia com tarja "SEM VALOR FISCAL").
    """
    try:
        root = ET.fromstring(xml_content.encode("utf-8"))
    except ET.ParseError as e:
        raise HTTPException(422, f"XML da nota inválido: {e}")

    inf = root.find(".//nfe:infNFe", NS)
    if inf is None:
        raise HTTPException(422, "XML sem infNFe.")

    chave = (inf.get("Id") or "").replace("NFe", "")
    if not re.fullmatch(r"\d{44}", chave):
        raise HTTPException(422, "Chave de acesso inválida no XML.")

    def txt(tag, escopo):
        if escopo is None:
            return None
        el = escopo.find(f".//nfe:{tag}", NS)
        return el.text.strip() if el is not None and el.text else None

    ide = inf.find("nfe:ide", NS)
    prot = root.find(".//nfe:protNFe", NS)
    c_stat = txt("cStat", prot)
    tp_amb = txt("tpAmb", ide)

    if c_stat not in STATUS_AUTORIZADOS:
        raise HTTPException(422, f"NF-e não autorizada (cStat={c_stat}).")
    if tp_amb != "1":
        raise HTTPException(422, f"XML de homologação (tpAmb={tp_amb}).")

    return {
        "chave": chave,
        "numero": txt("nNF", ide),
        "serie": txt("serie", ide),
    }


# ---------------------------------------------------------------- PDF
def gerar_pdf(xml_content: str) -> bytes:
    """
    XML -> bytes do PDF. Fonte TIMES de propósito: com COURIER o texto de
    consulta de autenticidade transborda e sobrepõe a linha do protocolo.
    output() sem argumento devolve bytearray (fpdf2 2.8 não tem dest="S").
    """
    from brazilfiscalreport.danfe import (
        Danfe,
        DanfeConfig,
        FontType,
        Margins,
        ReceiptPosition,
    )

    config = DanfeConfig(
        margins=Margins(top=5, right=5, bottom=5, left=5),
        receipt_pos=ReceiptPosition.TOP,
        font_type=FontType.TIMES,
    )
    logo = os.environ.get("LOGO_PATH")
    if logo and os.path.exists(logo):
        config.logo = logo

    pdf = bytes(Danfe(xml=xml_content, config=config).output())
    if not pdf.startswith(b"%PDF"):
        raise HTTPException(500, "Falha ao gerar o PDF.")
    return pdf


# ---------------------------------------------------------------- storage
# Dois back-ends. Escolha por STORAGE_BACKEND = "sftp" (Umbler) ou "s3" (R2/S3).
#
# SFTP (hospedagem Umbler):
#   STORAGE_BACKEND=sftp
#   SFTP_HOST=arquivos-bisturi-com-br.umbler.net
#   SFTP_PORT=22
#   SFTP_USER=umbler
#   SFTP_PASSWORD=...            (ou SFTP_KEY_PATH para chave SSH)
#   SFTP_BASE_DIR=/public_html/danfe
#   PUBLIC_BASE_URL=https://arquivos.bisturi.com.br/danfe
#
# ⚠️ LIMITAÇÃO DO SFTP: não existe URL assinada — o arquivo fica público para
# quem tiver o link, e o link não expira. O DANFE contém nome, endereço, CPF e
# itens comprados. Mitigações: (1) nome do arquivo é a chave da NF-e, com 8
# dígitos aleatórios, então não é adivinhável por tentativa; (2) rodar
# expurgar_antigos() periodicamente; (3) OBRIGATÓRIO colocar um .htaccess com
# "Options -Indexes" na pasta, senão qualquer um abre /danfe/ e lista tudo,
# e aí o nome imprevisível não protege nada.

STORAGE_BACKEND = os.environ.get("STORAGE_BACKEND", "sftp").lower()


import io
from ftplib import FTP, FTP_TLS, error_perm


def _sftp():
    import paramiko

    transport = paramiko.Transport(
        (os.environ["SFTP_HOST"], int(os.environ.get("SFTP_PORT", 22)))
    )
    key_path = os.environ.get("SFTP_KEY_PATH")
    if key_path:
        pkey = paramiko.RSAKey.from_private_key_file(key_path)
        transport.connect(username=os.environ["SFTP_USER"], pkey=pkey)
    else:
        transport.connect(
            username=os.environ["SFTP_USER"], password=os.environ["SFTP_PASSWORD"]
        )
    return paramiko.SFTPClient.from_transport(transport), transport


def _sftp_remote_path(chave: str) -> str:
    base = os.environ.get("SFTP_BASE_DIR", "/public_html/danfe").rstrip("/")
    return f"{base}/{chave}.pdf"


def _sftp_public_url(chave: str) -> str:
    base = os.environ.get(
        "PUBLIC_BASE_URL", "https://arquivos.bisturi.com.br/danfe"
    ).rstrip("/")
    return f"{base}/{chave}.pdf"


def _sftp_existe(chave: str) -> bool:
    sftp, transport = _sftp()
    try:
        sftp.stat(_sftp_remote_path(chave))
        return True
    except IOError:
        return False
    finally:
        sftp.close()
        transport.close()


def _sftp_subir(pdf_bytes: bytes, chave: str) -> str:
    sftp, transport = _sftp()
    try:
        remoto = _sftp_remote_path(chave)
        # cria a pasta se não existir
        pasta = os.path.dirname(remoto)
        try:
            sftp.stat(pasta)
        except IOError:
            sftp.mkdir(pasta)
        # grava em nome temporário e renomeia: evita que alguém baixe um PDF
        # pela metade se pedir a nota no exato momento do upload
        tmp = remoto + ".part"
        with sftp.file(tmp, "wb") as f:
            f.write(pdf_bytes)
        try:
            sftp.remove(remoto)
        except IOError:
            pass
        sftp.rename(tmp, remoto)
        return _sftp_public_url(chave)
    finally:
        sftp.close()
        transport.close()


class _FTPSReuse(FTP_TLS):
    """Reaproveita a sessão TLS no canal de dados — muitos servidores FTPS exigem."""
    def ntransfercmd(self, cmd, rest=None):
        conn, size = FTP.ntransfercmd(self, cmd, rest)
        if self._prot_p:
            conn = self.context.wrap_socket(
                conn, server_hostname=self.host, session=self.sock.session
            )
        return conn, size


def _ftps():
    ftps = _FTPSReuse()
    ftps.connect(os.environ["SFTP_HOST"], int(os.environ.get("FTPS_PORT", 21)), timeout=30)
    ftps.login(os.environ["SFTP_USER"], os.environ["SFTP_PASSWORD"])
    ftps.prot_p()          # criptografa o canal de dados
    ftps.set_pasv(True)    # passivo: obrigatório saindo de container
    return ftps


def _ftps_fechar(ftps):
    try:
        ftps.quit()
    except Exception:
        ftps.close()


def _ftps_cd(ftps, pasta: str, criar: bool = False):
    ftps.cwd("/")
    for parte in [p for p in pasta.split("/") if p]:
        try:
            ftps.cwd(parte)
        except error_perm:
            if not criar:
                raise
            ftps.mkd(parte)
            ftps.cwd(parte)


def _ftps_existe(chave: str) -> bool:
    ftps = _ftps()
    try:
        pasta, nome = _sftp_remote_path(chave).rsplit("/", 1)
        try:
            _ftps_cd(ftps, pasta)
        except error_perm:
            return False
        ftps.voidcmd("TYPE I")
        try:
            ftps.size(nome)
            return True
        except error_perm:
            return False
    finally:
        _ftps_fechar(ftps)


def _ftps_subir(pdf_bytes: bytes, chave: str) -> str:
    ftps = _ftps()
    try:
        pasta, nome = _sftp_remote_path(chave).rsplit("/", 1)
        _ftps_cd(ftps, pasta, criar=True)
        tmp = nome + ".part"
        ftps.storbinary(f"STOR {tmp}", io.BytesIO(pdf_bytes))
        try:
            ftps.delete(nome)
        except error_perm:
            pass
        ftps.rename(tmp, nome)
        return _sftp_public_url(chave)
    finally:
        _ftps_fechar(ftps)


def _s3():
    import boto3
    from botocore.config import Config

    return boto3.client(
        "s3",
        endpoint_url=os.environ.get("S3_ENDPOINT_URL")
        or f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        config=Config(signature_version="s3v4"),
        region_name="auto",
    )


def _s3_key(chave: str) -> str:
    return f"danfe/{chave}.pdf"


def _s3_existe(chave: str) -> bool:
    from botocore.exceptions import ClientError

    try:
        _s3().head_object(Bucket=os.environ["R2_BUCKET"], Key=_s3_key(chave))
        return True
    except ClientError:
        return False


def _s3_url(chave: str) -> str:
    """URL assinada com expiração — preferível ao SFTP quando disponível."""
    return _s3().generate_presigned_url(
        "get_object",
        Params={"Bucket": os.environ["R2_BUCKET"], "Key": _s3_key(chave)},
        ExpiresIn=int(os.environ.get("URL_EXPIRA_SEGUNDOS", 604800)),
    )


def _s3_subir(pdf_bytes: bytes, chave: str) -> str:
    _s3().put_object(
        Bucket=os.environ["R2_BUCKET"],
        Key=_s3_key(chave),
        Body=pdf_bytes,
        ContentType="application/pdf",
    )
    return _s3_url(chave)


# ---- interface usada pelo endpoint (independente do back-end) ----
def ja_existe(chave: str) -> bool:
    if STORAGE_BACKEND == "ftps":
        return _ftps_existe(chave)
    return _sftp_existe(chave) if STORAGE_BACKEND == "sftp" else _s3_existe(chave)


def url_publica(chave: str) -> str:
    if STORAGE_BACKEND in ("ftps", "sftp"):
        return _sftp_public_url(chave)
    return _s3_url(chave)


def subir_pdf(pdf_bytes: bytes, chave: str) -> str:
    if STORAGE_BACKEND == "ftps":
        return _ftps_subir(pdf_bytes, chave)
    return _sftp_subir(pdf_bytes, chave) if STORAGE_BACKEND == "sftp" else _s3_subir(pdf_bytes, chave)


def expurgar_antigos(dias: int = 7) -> int:
    """
    Apaga PDFs com mais de `dias` dias. Rode por cron.

    Importante no SFTP, onde o link não expira: reduz a janela em que uma nota
    fica acessível. Se o cliente pedir de novo depois, a bridge regenera.
    Devolve quantos arquivos foram apagados.
    """
    import time

    if STORAGE_BACKEND != "sftp":
        return 0

    limite = time.time() - dias * 86400
    sftp, transport = _sftp()
    apagados = 0
    try:
        base = os.environ.get("SFTP_BASE_DIR", "/public_html/danfe").rstrip("/")
        for entry in sftp.listdir_attr(base):
            if entry.filename.endswith(".pdf") and entry.st_mtime < limite:
                sftp.remove(f"{base}/{entry.filename}")
                apagados += 1
    finally:
        sftp.close()
        transport.close()
    return apagados


# ---------------------------------------------------------------- endpoint
@app.post("/danfe", response_model=RespostaOk)
def danfe(req: PedidoRequest, authorization: str = Header(None)):
    esperado = os.environ.get("BRIDGE_TOKEN")
    if not esperado or authorization != f"Bearer {esperado}":
        raise HTTPException(401, "Não autorizado.")

    pedido = buscar_pedido_vtex(req.orderId)
    encontrados = extrair_xmls(pedido, req.invoiceNumber)

    if not encontrados:
        # 409: pedido existe mas ainda não tem nota com XML. O agente deve
        # responder "sua nota ainda não foi emitida", não "erro".
        raise HTTPException(
            409, "Pedido sem nota fiscal disponível (ainda não emitida)."
        )

    notas = []
    for item in encontrados:
        info = validar_xml(item["xml"])
        chave = info["chave"]
        if ja_existe(chave):
            url = url_publica(chave)   # cache: não regenera o PDF
        else:
            url = subir_pdf(gerar_pdf(item["xml"]), chave)
        notas.append(
            NotaResponse(
                numero=info["numero"], serie=info["serie"], chave=chave, pdf_url=url
            )
        )

    return RespostaOk(orderId=req.orderId, notas=notas)


@app.get("/health")
def health():
    return {"status": "ok"}
