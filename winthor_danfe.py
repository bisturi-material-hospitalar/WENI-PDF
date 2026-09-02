"""
Fonte Winthor de XML de NF-e para a bridge DANFE.

Por que um módulo separado: tudo o que a bridge faz depois do XML já é
agnóstico de origem — validar_xml, gerar_pdf, subir_pdf, ja_existe,
url_publica e buscar_por_numero não sabem nem se importam de onde o XML veio.
Só as funções de BUSCA são da VTEX. Este arquivo acrescenta uma segunda
busca; nada do núcleo muda.

Integração no bridge_danfe.py — três pontos:

    # no topo, junto dos outros imports
    from winthor_danfe import pedido_do_winthor, extrair_xmls_winthor

    # em gerar_notas_do_pedido (era: encontrados = extrair_xmls(...))
    if pedido_do_winthor(order_id):
        encontrados = extrair_xmls_winthor(order_id, invoice_number)
    else:
        encontrados = extrair_xmls(buscar_pedido_vtex(order_id), invoice_number)

    # no endpoint /danfe, no gate do orderId
    if not pedido_do_site(req.orderId) and not pedido_do_winthor(req.orderId):
        raise HTTPException(400, "Pedido fora do escopo desta consulta.")

Variáveis de ambiente novas (mesmos valores já usados pelo agente de
consultas do Winthor):

    WINTHOR_URL, WINTHOR_LOGIN, WINTHOR_SENHA_MD5, WINTHOR_BRANCH_ID
"""

import os
import re
import time
from typing import List, Optional
from xml.etree import ElementTree as ET

import requests
from fastapi import HTTPException

NS = {"nfe": "http://www.portalfiscal.inf.br/nfe"}
TIMEOUT = 30

WINTHOR_URL = os.environ.get("WINTHOR_URL", "").rstrip("/")
WINTHOR_LOGIN = os.environ.get("WINTHOR_LOGIN", "")
WINTHOR_SENHA_MD5 = os.environ.get("WINTHOR_SENHA_MD5", "")
WINTHOR_BRANCH_ID = os.environ.get("WINTHOR_BRANCH_ID", "1")

# O pedido do site tem hífen (RE_PEDIDO_SITE = r"\d+-\d+"). O do Winthor é só
# dígitos, com o código do RCA como prefixo: 257000098 é RCA 257, 69323074 é
# RCA 69. Não validamos o RCA aqui — prefixo novo não deve derrubar a consulta.
#
# Piso de 8 dígitos de propósito: o número da NOTA tem 6 (ex. 373845), e os
# pedidos reais têm 8 e 9. Aceitar 6 faria um número de nota enviado por engano
# em order_id ser tratado como pedido, devolvendo "pedido não encontrado" em
# vez de localizar a nota.
RE_PEDIDO_WINTHOR = re.compile(r"\d{8,12}")

# Token vale por sessão do processo. O plano free do Render hiberna, então o
# processo é novo com frequência e o cache raramente passa de alguns minutos —
# mas evita um login por consulta durante um pico.
_TOKEN_CACHE = {"valor": None, "expira_em": 0.0}
_TOKEN_TTL = 600.0


def pedido_do_winthor(order_id: str) -> bool:
    return bool(RE_PEDIDO_WINTHOR.fullmatch((order_id or "").strip()))


def rca_do_pedido(order_id: str) -> Optional[str]:
    """
    Código do RCA que originou o pedido, pelo prefixo do número.

    Serve para log e para distinguir origem: 69 é o e-commerce do site,
    257 é o atendimento automatizado. Não é usado para decidir nada — o
    prefixo pode mudar sem aviso quando um RCA novo é cadastrado.
    """
    num = (order_id or "").strip()
    return num[:3] if len(num) >= 6 else None


def _headers() -> dict:
    agora = time.time()
    if _TOKEN_CACHE["valor"] and agora < _TOKEN_CACHE["expira_em"]:
        token = _TOKEN_CACHE["valor"]
    else:
        resp = requests.post(
            f"{WINTHOR_URL}/winthor/autenticacao/v1/login",
            json={"login": WINTHOR_LOGIN, "senha": WINTHOR_SENHA_MD5},
            timeout=TIMEOUT,
        )
        if resp.status_code != 200:
            raise HTTPException(
                502, f"Falha de autenticação no Winthor (HTTP {resp.status_code})."
            )
        token = resp.json().get("accessToken") or ""
        if not token:
            raise HTTPException(502, "Winthor não devolveu accessToken.")
        _TOKEN_CACHE.update(valor=token, expira_em=agora + _TOKEN_TTL)

    # O Winthor usa o token cru no Authorization, sem o prefixo "Bearer".
    return {"Authorization": token, "Accept": "application/json"}


def _status_do_pedido(order_id: str, headers: dict) -> Optional[str]:
    """
    Status do pedido, procurando em TODAS as filiais configuradas.

    O endpoint de pedido individual aceita um branchId só, e a Bisturi tem três
    lojas emitindo. Consultar apenas a primeira faz pedido de outra filial
    parecer inexistente — é o furo que existe hoje no agente de consultas, que
    usa WINTHOR_BRANCH_ID.split(",")[0].

    Devolve o código de status ("F" = faturado) ou None se o pedido não existir
    em nenhuma filial.
    """
    filiais = [f.strip() for f in WINTHOR_BRANCH_ID.split(",") if f.strip()]
    encontrado = None
    for filial in filiais:
        try:
            resp = requests.get(
                f"{WINTHOR_URL}/api/wholesale/v1/orders/",
                headers=headers,
                params={"orderId": order_id, "branchId": filial},
                timeout=TIMEOUT,
            )
        except requests.exceptions.RequestException:
            continue
        if resp.status_code != 200:
            continue
        status = (resp.json() or {}).get("orderStatus") or ""
        if status == "F":
            return "F"          # faturado: para de procurar
        encontrado = encontrado or status
    return encontrado


def _nnf_do_xml(xml: str) -> Optional[str]:
    try:
        root = ET.fromstring(xml.encode("utf-8"))
    except ET.ParseError:
        return None
    el = root.find(".//nfe:ide/nfe:nNF", NS)
    return el.text.strip() if el is not None and el.text else None


def extrair_xmls_winthor(
    order_id: str, invoice_number: Optional[str] = None
) -> List[dict]:
    """
    Pedido do Winthor -> [{"xml": ..., "invoiceNumber": ...}].

    Mesma forma de retorno de extrair_xmls, para gerar_notas_do_pedido tratar
    as duas origens sem saber a diferença.

    Contratos de erro, iguais aos do caminho VTEX:
      - pedido inexistente em todas as filiais -> 404 (número errado)
      - pedido existe e não está faturado      -> [] , que virá como 409
        ("nota ainda não emitida") lá no endpoint
      - falha de rede ou de auth               -> 502
    """
    if not WINTHOR_URL:
        raise HTTPException(503, "Consulta ao Winthor não configurada.")

    headers = _headers()
    status = _status_do_pedido(order_id, headers)

    if status is None:
        raise HTTPException(404, f"Pedido {order_id} não localizado.")
    if status != "F":
        return []               # existe, mas ainda não faturado

    try:
        resp = requests.get(
            f"{WINTHOR_URL}/winthor/fiscal/v1/documentosfiscais/nfe/invoiceDocument",
            headers=headers,
            params={"orderId": order_id, "returnBase64": "false"},
            timeout=TIMEOUT,
        )
    except requests.exceptions.RequestException as erro:
        raise HTTPException(502, f"Winthor não respondeu: {type(erro).__name__}")

    if resp.status_code == 404:
        return []               # faturado sem documento fiscal ainda publicado
    if resp.status_code != 200:
        raise HTTPException(
            502, f"Erro ao buscar NF-e no Winthor (HTTP {resp.status_code})."
        )

    xml = (resp.json() or {}).get("invoiceXml") or ""
    if not xml.strip():
        return []

    numero = _nnf_do_xml(xml)

    # Se a consulta veio pelo número da nota, confere que é esta mesma. Sem
    # isso, um pedido com nota diferente da pedida devolveria o documento
    # errado — no caminho VTEX esse filtro existe dentro do extrair_xmls.
    if invoice_number:
        pedido_num = str(invoice_number).strip().lstrip("0")
        if (numero or "").lstrip("0") != pedido_num:
            return []

    return [{"xml": xml, "invoiceNumber": numero or ""}]
