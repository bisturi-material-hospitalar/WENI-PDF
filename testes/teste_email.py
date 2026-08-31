"""
Testa buscar_notas_por_email com uma resposta de listagem da VTEX simulada.

O que precisa ficar provado aqui:
  1. pedido de outro CNPJ nunca entra na lista, mesmo tendo vindo na busca
  2. pedido com prefixo de letras é descartado
  3. pedido sem invoiceOutput é descartado
  4. pedido fora da janela de dias corta a varredura
  5. e-mail mascarado pela VTEX não invalida um documento que bate
  6. e-mail em claro divergente invalida o pedido
  7. CNPJ no campo corporateDocument também vale
  8. um pedido que não abre na VTEX não derruba a busca inteira
  9. o teto de EMAIL_MAX_ABRIR limita as chamadas HTTP
"""
import os
import pathlib
import sys
from datetime import datetime, timedelta, timezone

os.environ.setdefault("VTEX_ACCOUNT", "tfchmj")
os.environ.setdefault("VTEX_APP_KEY", "x")
os.environ.setdefault("VTEX_APP_TOKEN", "x")
os.environ.setdefault("BRIDGE_TOKEN", "x")
# a bridge fica na pasta acima desta
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import bridge_danfe as b  # noqa: E402
from fastapi import HTTPException  # noqa: E402

HOJE = datetime.now(timezone.utc)


def iso(dias_atras):
    d = HOJE - timedelta(days=dias_atras)
    return d.strftime("%Y-%m-%dT%H:%M:%S.0000000+00:00")


EMAIL = "cliente@empresa.com.br"
CNPJ = "32561144000103"
OUTRO_CNPJ = "11222333000181"

LISTA = [
    # 0: bom, recente
    {"orderId": "1657541006231-01", "invoiceOutput": ["372457"],
     "creationDate": iso(3), "totalValue": 123456},
    # 1: prefixo de letras -> descartado sem abrir
    {"orderId": "PGM-1657548196767-01", "invoiceOutput": ["372999"],
     "creationDate": iso(4), "totalValue": 100},
    # 2: sem nota emitida -> descartado sem abrir
    {"orderId": "1657541006232-01", "invoiceOutput": [],
     "creationDate": iso(5), "totalValue": 100},
    # 3: outro CNPJ -> abre e é recusado
    {"orderId": "1657541006233-01", "invoiceOutput": ["372500"],
     "creationDate": iso(6), "totalValue": 5000},
    # 4: e-mail em claro divergente -> abre e é recusado
    {"orderId": "1657541006234-01", "invoiceOutput": ["372501"],
     "creationDate": iso(7), "totalValue": 5000},
    # 5: e-mail mascarado pela VTEX, CNPJ bate -> aceito
    {"orderId": "1657541006235-01", "invoiceOutput": ["372502"],
     "creationDate": iso(8), "totalValue": 89000},
    # 6: CNPJ em corporateDocument -> aceito
    {"orderId": "1657541006236-01", "invoiceOutput": ["372503"],
     "creationDate": iso(9), "totalValue": 241090},
    # 7: pedido que não abre na VTEX -> ignorado, sem derrubar a busca
    {"orderId": "1657541006237-01", "invoiceOutput": ["372504"],
     "creationDate": iso(10), "totalValue": 100},
    # 8: duas notas no mesmo pedido -> duas opções
    {"orderId": "1657541006238-01", "invoiceOutput": ["372505", "372506"],
     "creationDate": iso(11), "totalValue": 700000},
    # 9: fora da janela -> corta aqui
    {"orderId": "1657541006239-01", "invoiceOutput": ["300000"],
     "creationDate": iso(400), "totalValue": 100},
    # 10: depois do corte, nunca deve ser visto
    {"orderId": "1657541006240-01", "invoiceOutput": ["300001"],
     "creationDate": iso(401), "totalValue": 100},
]

PERFIS = {
    "1657541006231-01": {"email": EMAIL, "document": CNPJ},
    "1657541006233-01": {"email": EMAIL, "document": OUTRO_CNPJ},
    "1657541006234-01": {"email": "outro@empresa.com.br", "document": CNPJ},
    "1657541006235-01": {"email": "a1b2c3-d4e5@ct.vtex.com.br", "document": CNPJ},
    "1657541006236-01": {"email": EMAIL, "document": "", "corporateDocument": CNPJ},
    "1657541006238-01": {"email": EMAIL, "document": CNPJ},
}

ABERTOS = []


class RespFalsa:
    status_code = 200

    def __init__(self, corpo):
        self._c = corpo

    def json(self):
        return self._c


def get_falso(url, params=None, headers=None, timeout=None):
    assert params.get("q") == EMAIL, params
    assert params.get("f_status") == "invoiced", params
    return RespFalsa({"list": LISTA})


def pedido_falso(order_id):
    ABERTOS.append(order_id)
    if order_id not in PERFIS:
        raise HTTPException(502, "VTEX retornou 500.")
    return {"clientProfileData": PERFIS[order_id]}


b.requests.get = get_falso
b.buscar_pedido_vtex = pedido_falso

falhas = []
opcoes = b.buscar_notas_por_email(EMAIL, "32.561.144/0001-03")
numeros = [o.numero for o in opcoes]

esperado = ["372457", "372502", "372503", "372505", "372506"]
if numeros != esperado:
    falhas.append("números: obtido=%r esperado=%r" % (numeros, esperado))

# nenhum pedido descartado por regra local foi aberto na VTEX
for nao_deve in ("PGM-1657548196767-01", "1657541006232-01",
                 "1657541006239-01", "1657541006240-01"):
    if nao_deve in ABERTOS:
        falhas.append("abriu na VTEX um pedido que devia ser descartado antes: %s"
                      % nao_deve)

# o pedido do outro CNPJ foi aberto (a conferência exige) mas não entrou
if "1657541006233-01" not in ABERTOS:
    falhas.append("não conferiu o pedido de outro CNPJ")
if "372500" in numeros:
    falhas.append("VAZOU nota de outro CNPJ")
if "372501" in numeros:
    falhas.append("VAZOU nota com e-mail em claro divergente")

# data e valor formatados
primeira = opcoes[0]
if primeira.valor != "R$ 1.234,56":
    falhas.append("valor da primeira opção: %r" % primeira.valor)
if primeira.data_pedido != (HOJE - timedelta(days=3)).strftime("%d/%m/%Y"):
    falhas.append("data da primeira opção: %r" % primeira.data_pedido)
if primeira.orderId != "1657541006231-01":
    falhas.append("orderId da primeira opção: %r" % primeira.orderId)

# ---- documento que não bate com nada devolve lista vazia ----
ABERTOS.clear()
vazio = b.buscar_notas_por_email(EMAIL, "111.444.777-35")
if vazio:
    falhas.append("documento sem correspondência devolveu %d opções" % len(vazio))

# ---- teto de EMAIL_MAX_OPCOES ----
ABERTOS.clear()
b.EMAIL_MAX_OPCOES = 2
duas = b.buscar_notas_por_email(EMAIL, CNPJ)
if len(duas) != 2:
    falhas.append("teto de opções não respeitado: %d" % len(duas))
b.EMAIL_MAX_OPCOES = 8

# ---- teto de EMAIL_MAX_ABRIR limita chamadas HTTP ----
ABERTOS.clear()
b.EMAIL_MAX_ABRIR = 2
b.buscar_notas_por_email(EMAIL, CNPJ)
if len(ABERTOS) > 2:
    falhas.append("teto de aberturas não respeitado: %d chamadas" % len(ABERTOS))
b.EMAIL_MAX_ABRIR = 20

# ---- janela de dias ----
ABERTOS.clear()
b.EMAIL_JANELA_DIAS = 5
curta = b.buscar_notas_por_email(EMAIL, CNPJ)
if [o.numero for o in curta] != ["372457"]:
    falhas.append("janela de 5 dias: %r" % [o.numero for o in curta])
b.EMAIL_JANELA_DIAS = 180

if falhas:
    print("FALHAS (%d):" % len(falhas))
    for f in falhas:
        print("  - " + f)
    raise SystemExit(1)
print("bridge/e-mail: todos os casos passaram")
print("  notas devolvidas:", numeros)
print("  pedidos abertos na VTEX:", len(PERFIS) + 1, "de", len(LISTA), "listados")
