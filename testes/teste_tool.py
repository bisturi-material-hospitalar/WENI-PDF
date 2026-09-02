"""
Exercita a tool Get Danfe sem a plataforma Weni e sem rede: injeta stubs de
`weni` e substitui requests.post por respostas controladas.
"""
import os
import pathlib
import sys
import types

# ---- stubs do SDK da Weni ----
weni = types.ModuleType("weni")


class Tool:
    pass


weni.Tool = Tool
ctx = types.ModuleType("weni.context")


class Context:
    def __init__(self, parameters):
        self.parameters = parameters


ctx.Context = Context
resp_mod = types.ModuleType("weni.responses")


class TextResponse:
    def __init__(self, data):
        self.data = data


resp_mod.TextResponse = TextResponse
sys.modules["weni"] = weni
sys.modules["weni.context"] = ctx
sys.modules["weni.responses"] = resp_mod

# a tool vive no repo weni-agent, irmão deste. Dá para apontar outro
# lugar com  WENI_AGENT_DIR=...  antes de rodar.
raiz = pathlib.Path(os.environ.get(
    "WENI_AGENT_DIR",
    pathlib.Path(__file__).resolve().parent.parent.parent / "weni-agent",
))
sys.path.insert(0, str(raiz / "tools" / "get_danfe"))
import main  # noqa: E402

# ---- dublê de requests ----
CHAMADAS = []
PROGRAMADO = []


class RespostaFalsa:
    def __init__(self, status, corpo):
        self.status_code = status
        self._corpo = corpo

    def json(self):
        return self._corpo


def post_falso(url, json=None, headers=None, timeout=None):
    CHAMADAS.append(json)
    if not PROGRAMADO:
        raise AssertionError("chamada à bridge não esperada: %r" % (json,))
    return PROGRAMADO.pop(0)


main.requests.post = post_falso

falhas = []


def caso(rotulo, parametros, respostas, espera_motivo=None, espera_ok=None,
         espera_corpo=None, espera_chamadas=None):
    CHAMADAS.clear()
    PROGRAMADO.clear()
    PROGRAMADO.extend(respostas)
    saida = main.GetDanfe().execute(Context(parametros)).data

    if espera_ok is not None and saida.get("ok") is not espera_ok:
        falhas.append("%s: ok=%r esperado %r" % (rotulo, saida.get("ok"), espera_ok))
    if espera_motivo is not None and saida.get("motivo") != espera_motivo:
        falhas.append(
            "%s: motivo=%r esperado %r" % (rotulo, saida.get("motivo"), espera_motivo)
        )
    if espera_corpo is not None and CHAMADAS[:1] != [espera_corpo]:
        falhas.append(
            "%s: corpo enviado=%r esperado %r"
            % (rotulo, CHAMADAS[:1], [espera_corpo])
        )
    if espera_chamadas is not None and len(CHAMADAS) != espera_chamadas:
        falhas.append(
            "%s: %d chamadas à bridge, esperado %d"
            % (rotulo, len(CHAMADAS), espera_chamadas)
        )
    return saida


# ---------------------------------------------------------------- e-mail
# passo 1 do fluxo em duas perguntas: e-mail sozinho devolve a pergunta do
# CPF/CNPJ, sem tocar na bridge
p1 = caso(
    "e-mail sem documento: pede o CPF/CNPJ sem chamar a bridge",
    {"email": "cliente@empresa.com.br"},
    [],
    espera_motivo="falta_documento",
    espera_ok=False,
    espera_chamadas=0,
)
if "e-mail" in p1.get("mensagem", "").lower():
    falhas.append("pergunta do CPF/CNPJ pede o e-mail de novo: %r"
                  % p1.get("mensagem"))

# passo 2: com os dois, vai para a bridge
caso(
    "e-mail + documento: agora consulta",
    {"email": "cliente@empresa.com.br", "documento": "32561144000103"},
    [RespostaFalsa(404, {})],
    espera_motivo="email_nao_encontrado",
    espera_corpo={"email": "cliente@empresa.com.br",
                  "documento": "32561144000103"},
    espera_chamadas=1,
)

# a oferta inicial não pode anunciar o CPF/CNPJ junto com o e-mail
oferta = caso(
    "oferta inicial não expõe o pedido conjunto",
    {},
    [],
    espera_motivo="sem_identificador",
    espera_chamadas=0,
)
msg = oferta.get("mensagem", "").lower()
for proibido in ("cpf", "cnpj"):
    if proibido in msg:
        falhas.append("oferta inicial menciona %s: %r" % (proibido, msg))

caso(
    "e-mail malformado é barrado antes do documento",
    {"email": "cliente", "documento": "32561144000103"},
    [],
    espera_motivo="email_invalido",
    espera_chamadas=0,
)

caso(
    "documento curto é barrado local",
    {"email": "cliente@empresa.com.br", "documento": "123456"},
    [],
    espera_motivo="documento_invalido",
    espera_chamadas=0,
)

caso(
    "documento com DV errado: 422 da bridge",
    {"email": "cliente@empresa.com.br", "documento": "32.561.144/0001-04"},
    [RespostaFalsa(422, {})],
    espera_motivo="documento_invalido",
    espera_corpo={"email": "cliente@empresa.com.br", "documento": "32561144000104"},
)

caso(
    "e-mail sem compra: 404 vira email_nao_encontrado",
    {"email": "cliente@empresa.com.br", "documento": "32.561.144/0001-03"},
    [RespostaFalsa(404, {})],
    espera_motivo="email_nao_encontrado",
)

# --- uma nota só: a bridge já entrega o PDF ---
uma = caso(
    "e-mail com uma nota: entrega direto no formato padrão",
    {"email": "cliente@empresa.com.br", "documento": "32561144000103"},
    [
        RespostaFalsa(
            200,
            {
                "orderId": "1657541006231-01",
                "notas": [
                    {
                        "numero": "372457",
                        "serie": "50",
                        "chave": "3" * 44,
                        "pdf_url": "https://arquivos.bisturi.com.br/danfe/%s.pdf"
                        % ("3" * 44),
                        "emissao": "28/08/2026",
                    }
                ],
            },
        )
    ],
    espera_ok=True,
    espera_corpo={"email": "cliente@empresa.com.br", "documento": "32561144000103"},
)
esperado = (
    "A nota fiscal deste pedido é 372457, série 50.\n"
    "Chave de acesso: %s\n"
    "Data de emissão: 28/08/2026\n"
    "Link do PDF da DANFE: https://arquivos.bisturi.com.br/danfe/%s.pdf"
    % ("3" * 44, "3" * 44)
)
if uma.get("mensagem") != esperado:
    falhas.append("mensagem de 4 linhas mudou:\n%r" % uma.get("mensagem"))

# --- várias notas: lista de opções ---
varias = caso(
    "e-mail com três notas: devolve lista",
    {"email": "cliente@empresa.com.br", "documento": "32561144000103"},
    [
        RespostaFalsa(
            200,
            {
                "email": "cliente@empresa.com.br",
                "notas": [],
                "opcoes": [
                    {
                        "numero": "372457",
                        "orderId": "1657541006231-01",
                        "data_pedido": "28/08/2026",
                        "valor": "R$ 1.234,56",
                    },
                    {
                        "numero": "372206",
                        "orderId": "1657391006010-01",
                        "data_pedido": "21/08/2026",
                        "valor": "R$ 890,00",
                    },
                    {
                        "numero": "371006",
                        "orderId": "1657161005600-01",
                        "data_pedido": "05/08/2026",
                        "valor": "R$ 2.410,90",
                    },
                ],
            },
        )
    ],
    espera_motivo="varias_notas",
    espera_ok=False,
)
if varias.get("numeros") != ["372457", "372206", "371006"]:
    falhas.append("campo numeros errado: %r" % varias.get("numeros"))
lista_esperada = (
    "Encontrei 3 notas fiscais no seu cadastro:\n"
    "\n"
    "1) Nota 372457 — pedido de 28/08/2026 — R$ 1.234,56\n"
    "2) Nota 372206 — pedido de 21/08/2026 — R$ 890,00\n"
    "3) Nota 371006 — pedido de 05/08/2026 — R$ 2.410,90\n"
    "\n"
    "Qual delas você quer?"
)
if varias.get("mensagem") != lista_esperada:
    falhas.append("lista de opções:\n%s" % varias.get("mensagem"))

# --- opção sem data/valor não quebra a linha ---
magra = caso(
    "opção sem data e sem valor",
    {"email": "c@e.com.br", "documento": "32561144000103"},
    [
        RespostaFalsa(
            200,
            {
                "opcoes": [
                    {"numero": "372457", "orderId": "1657541006231-01"},
                    {"numero": "372206", "orderId": "1657391006010-01",
                     "data_pedido": "21/08/2026"},
                ]
            },
        )
    ],
    espera_motivo="varias_notas",
)
if "1) Nota 372457\n" not in magra.get("mensagem", ""):
    falhas.append("opção magra: %r" % magra.get("mensagem"))

# ---------------------------------------------------------------- precedência
caso(
    "pedido tem precedência sobre nota e e-mail",
    {
        "order_id": "1657161005600-01",
        "numero_nota": "372457",
        "email": "c@e.com.br",
        "documento": "32561144000103",
    },
    [RespostaFalsa(409, {})],
    espera_motivo="pedido_sem_nota",
    espera_corpo={"orderId": "1657161005600-01"},
)

caso(
    "nota tem precedência sobre e-mail",
    {"numero_nota": "372457", "email": "c@e.com.br", "documento": "32561144000103"},
    [RespostaFalsa(404, {})],
    espera_motivo="nota_nao_encontrada",
    espera_corpo={"invoiceNumber": "372457"},
)

# ---------------------------------------------------------------- regressões
# nota de marketplace: o número está certo, a mensagem precisa dizer isso
mkt = caso(
    "nota de marketplace: 400 no caminho da nota",
    {"numero_nota": "372824"},
    [RespostaFalsa(400, {})],
    espera_motivo="fora_do_escopo",
)
if "está correto" not in mkt.get("mensagem", ""):
    falhas.append("mensagem de marketplace não afirma que o número está certo: %r"
                  % mkt.get("mensagem"))
if "confira" in mkt.get("mensagem", "").lower():
    falhas.append("mensagem de marketplace ainda manda conferir o número")

caso(
    "pedido com prefixo continua fora_do_escopo",
    {"order_id": "PGM-1657548196767-01"},
    [RespostaFalsa(400, {})],
    espera_motivo="fora_do_escopo",
)

caso(
    "série continua sendo enviada",
    {"numero_nota": "372457", "serie": "50"},
    [RespostaFalsa(404, {})],
    espera_motivo="nota_nao_encontrada",
    espera_corpo={"invoiceNumber": "372457", "serie": "50"},
)

caso(
    "dois documentos com o mesmo número: escalona, não pergunta série",
    {"numero_nota": "372457"},
    [
        RespostaFalsa(
            200,
            {
                "notas": [
                    {"numero": "372457", "serie": "50", "chave": "3" * 44,
                     "pdf_url": "u1"},
                    {"numero": "372457", "serie": "60", "chave": "4" * 44,
                     "pdf_url": "u2"},
                ]
            },
        )
    ],
    espera_motivo="nota_ambigua",
)

caso(
    "5xx faz retry e depois desiste",
    {"numero_nota": "372457"},
    [RespostaFalsa(503, {}), RespostaFalsa(503, {})],
    espera_motivo="http_503",
    espera_chamadas=2,
)

caso(
    "401 não faz retry",
    {"numero_nota": "372457"},
    [RespostaFalsa(401, {})],
    espera_motivo="http_401",
    espera_chamadas=1,
)

if falhas:
    print("FALHAS (%d):" % len(falhas))
    for f in falhas:
        print("  - " + f)
    raise SystemExit(1)
print("tool: todos os casos passaram")
