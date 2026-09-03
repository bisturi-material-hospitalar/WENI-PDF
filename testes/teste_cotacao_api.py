"""Teste local das rotas de cotacao, com o SFTP substituido por um dicionario.

Roda sem servidor, sem Umbler e sem Render. O que ele cobre:
  - token obrigatorio
  - numero gerado quando o chamador nao manda, e formato COT-DDMM-XXXX
  - numero recusado quando tem barra, ponto ou ".." (travessia de caminho)
  - nome do arquivo = "{numero}-{token}.pdf" e o token nao vem do numero
  - reaproveitamento dentro da validade e regeracao quando pedida
  - vencida: 200 com expirado=true e sem URL
  - inexistente: 404
"""

import os
import sys
import time

# O teste vive em testes/ e o modulo na raiz do repositorio. Sem isto, rodar
# "py testes\\teste_cotacao_api.py" da raiz falha no import: o sys.path[0] passa a ser
# a pasta testes/, nao a raiz.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ["BRIDGE_TOKEN"] = "token-de-teste"

import cotacao_api as api

AUTH = "Bearer token-de-teste"

# ------------------------------------------------------- SFTP falso, em memoria
ARQUIVOS = {}  # nome -> mtime


def subir_falso(pdf_bytes: bytes, nome: str) -> str:
    assert pdf_bytes[:4] == b"%PDF", "nao gerou PDF"
    assert len(pdf_bytes) > 5000, f"PDF suspeito de vazio: {len(pdf_bytes)} bytes"
    ARQUIVOS[nome] = time.time()
    return f"{api.PUBLIC_BASE_URL}/{nome}"


def procurar_falso(numero: str):
    from datetime import datetime

    prefixo = f"{numero}-"
    achados = [n for n in ARQUIVOS if n.startswith(prefixo) and n.endswith(".pdf")]
    if not achados:
        return None
    nome = max(achados, key=lambda n: ARQUIVOS[n])
    idade = (time.time() - ARQUIVOS[nome]) / 3600.0
    return {
        "nome": nome,
        "url": f"{api.PUBLIC_BASE_URL}/{nome}",
        "idade_horas": round(idade, 2),
        "expirado": idade > api.VALIDADE_HORAS,
        "gerado_em": datetime.fromtimestamp(ARQUIVOS[nome], api.FUSO).isoformat(),
    }


api.subir = subir_falso
api.procurar = procurar_falso

# ------------------------------------------------------------------ corpo base
BASE = {
    "cliente": "CLIENTE DE TESTE LTDA",
    "telefone": "21900000000",
    "codigo_cliente": "999999",
    "endereco": "RUA EXEMPLO",
    "numero_endereco": "56",
    "bairro": "CENTRO",
    "complemento": "Casa",
    "cidade": "RIO DE JANEIRO",
    "uf": "RJ",
    "cep": "20000000",
    "cpf_cnpj": "000.000.000-00",
    "ie": "ISENTO",
    "itens": [
        {"codigo": "19561", "descricao": "AGULHA PEN INSULINA 04MM WILTEX",
         "quantidade": 100, "unitario": 0.40, "disponivel": 100},
        {"codigo": "14846", "descricao": "LUVA PROCEDIMENTOS SEM PO TAM P - 100 UN",
         "quantidade": 10, "unitario": 49.90, "disponivel": 1},
    ],
    "pendentes": [
        {"descricao": "luva cirurgica 9.0", "quantidade": 12,
         "motivo": "voce pediu 9.0 e o que temos e 7.0 ou 8.5",
         "alternativas": [{"descricao": "LUVA CIRURGICA MEDIX 8.5 - PAR", "unitario": 2.90}]}
    ],
    "nao_localizados": ["tomografo portatil xyz"],
}


def req(**extra):
    dados = dict(BASE)
    dados.update(extra)
    return api.CotacaoRequest(**dados)


def esperar_erro(status, funcao, *args, **kwargs):
    from fastapi import HTTPException

    try:
        funcao(*args, **kwargs)
    except HTTPException as exc:
        assert exc.status_code == status, f"esperado {status}, veio {exc.status_code}"
        return exc
    raise AssertionError(f"esperado HTTP {status}, nao houve erro")


falhas = []


def checar(nome, condicao, detalhe=""):
    print(("  ok  " if condicao else " FALHA ") + nome + (f"  {detalhe}" if detalhe else ""))
    if not condicao:
        falhas.append(nome)


print("== autenticacao ==")
esperar_erro(401, api.criar_cotacao, req(), None)
esperar_erro(401, api.criar_cotacao, req(), "Bearer errado")
checar("sem token nao passa", True)

print("\n== numero gerado ==")
r1 = api.criar_cotacao(req(), AUTH)
import re
checar("formato COT-DDMM-XXXX", bool(re.fullmatch(r"COT-\d{4}-[A-Z2-9]{4}", r1.numero)), r1.numero)
checar("nome do arquivo tem numero + token",
       bool(re.fullmatch(rf"{re.escape(r1.numero)}-[A-Za-z0-9_-]{{16}}\.pdf",
                         r1.pdf_url.rsplit("/", 1)[-1])),
       r1.pdf_url.rsplit("/", 1)[-1])
checar("nao veio marcado como reaproveitado", r1.reaproveitado is False)

r2 = api.criar_cotacao(req(), AUTH)
checar("dois numeros gerados sao diferentes", r1.numero != r2.numero, f"{r1.numero} / {r2.numero}")

print("\n== numero informado pelo chamador ==")
r3 = api.criar_cotacao(req(numero="WA-9217"), AUTH)
checar("numero preservado", r3.numero == "WA-9217", r3.numero)
token_1 = r3.pdf_url.rsplit("/", 1)[-1]

print("\n== reaproveitamento ==")
r4 = api.criar_cotacao(req(numero="WA-9217"), AUTH)
checar("dentro da validade reaproveita", r4.reaproveitado is True)
checar("mesma URL", r4.pdf_url == r3.pdf_url)

r5 = api.criar_cotacao(req(numero="WA-9217", regerar=True), AUTH)
checar("regerar=True cria arquivo novo", r5.pdf_url != r3.pdf_url)
checar("token novo, nao derivado do numero",
       r5.pdf_url.rsplit("/", 1)[-1] != token_1)

print("\n== busca pelo numero ==")
b = api.buscar_cotacao("WA-9217", AUTH)
checar("acha sem regerar", b.pdf_url == r5.pdf_url and b.expirado is False)
checar("minusculo tambem acha", api.buscar_cotacao("wa-9217", AUTH).numero == "WA-9217")
esperar_erro(404, api.buscar_cotacao, "WA-0001", AUTH)
checar("inexistente da 404", True)

print("\n== validade ==")
nome_antigo = [n for n in ARQUIVOS if n.startswith("WA-9217-")]
for n in nome_antigo:
    ARQUIVOS[n] = time.time() - 25 * 3600
v = api.buscar_cotacao("WA-9217", AUTH)
checar("25h vira expirado", v.expirado is True, f"idade {v.idade_horas}h")
checar("expirada nao devolve URL", v.pdf_url == "")
e = api.existe_cotacao("WA-9217", AUTH, incluir_url=True)
checar("existe=True mesmo vencida", e["existe"] is True and e["expirado"] is True)
checar("incluir_url devolve o link vencido", "pdf_url" in e)
r6 = api.criar_cotacao(req(numero="WA-9217"), AUTH)
checar("vencida gera de novo sem pedir", r6.reaproveitado is False)

print("\n== numero invalido ==")
for ruim in ["../../etc/passwd", "WA/9217", "WA 9217", "WA.9217", "a" * 40,
             "WA-9217.pdf", "%2e%2e/x"]:
    esperar_erro(400, api.criar_cotacao, req(numero=ruim), AUTH)
checar("barra, ponto, espaco e travessia recusados", True)
# String vazia nao e numero invalido: e ausencia de numero, e a bridge gera um.
vazio = api.criar_cotacao(req(numero=""), AUTH)
checar("numero vazio gera um novo", vazio.numero.startswith("COT-"), vazio.numero)

print("\n== cotacao sem itens ==")
esperar_erro(400, api.criar_cotacao, req(itens=[]), AUTH)
checar("sem itens recusa", True)

print("\n== totais calculados na bridge ==")
dados = api._para_renderizador(req(numero="WA-1000"), "WA-1000")
checar("total pedido", abs(dados["total_pedido"] - (100 * 0.40 + 10 * 49.90)) < 0.001,
       f"{dados['total_pedido']:.2f}")
checar("total disponivel", abs(dados["total_disponivel"] - (100 * 0.40 + 1 * 49.90)) < 0.001,
       f"{dados['total_disponivel']:.2f}")
checar("numero da casa nao virou numero da cotacao",
       dados["numero"] == "56" and dados["protocolo"] == "WA-1000")

print("\n== fixture sem dado real de cliente ==")
# O repositorio weni-pdf e PUBLICO: blob commitado fica no historico para sempre. Em
# 03/09 esta fixture carregava nome, CPF, endereco e telefone de uma cliente real.
# Estas duas asseveracoes existem para a proxima pessoa nao repetir.
checar("nome da fixture marcado como teste", "TESTE" in BASE["cliente"].upper(),
       BASE["cliente"])
checar("documento da fixture e mascara, nao CPF/CNPJ real",
       set(BASE["cpf_cnpj"]) <= set("0./-"), BASE["cpf_cnpj"])

print()
if falhas:
    print(f"FALHOU: {len(falhas)} -> {falhas}")
    raise SystemExit(1)
print(f"TUDO OK  ({len(ARQUIVOS)} arquivos no storage falso)")
