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

Duas fontes de XML, escolhidas pelo formato do número do pedido:

    pedido do site (1657161005600-01) -> VTEX, campo
        packageAttachment.packages[].embeddedInvoice
    pedido do ERP  (257000098)        -> WinThor, endpoint
        winthor/fiscal/v1/documentosfiscais/nfe/invoiceDocument

A fonte WinThor fica em winthor_danfe.py. Tudo o que vem depois do XML —
validação, geração do PDF, publicação e busca no acervo — é comum às duas.

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
    WINTHOR_URL           base da API do WinThor (sem barra no fim)
    WINTHOR_LOGIN         usuário da API
    WINTHOR_SENHA_MD5     senha em MD5, como a API exige
    WINTHOR_BRANCH_ID     filiais separadas por vírgula (ex.: 1,2,3)

STATUS DOS TESTES
  TESTADO ✅  extração do embeddedInvoice, validação, geração do PDF e o
              tratamento de múltiplas notas por pedido (ver test_bridge.py)
  NÃO TESTADO ⚠️  chamada real à VTEX e upload real no R2 (precisam credenciais)
"""

import io
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional

import requests
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel

from winthor_danfe import extrair_xmls_winthor, pedido_do_winthor

NS = {"nfe": "http://www.portalfiscal.inf.br/nfe"}
STATUS_AUTORIZADOS = {"100", "150"}
TIMEOUT = 30

# Pedido do site: só dígitos e o sufixo -NN. Pedido com prefixo de letras
# (ex.: PGM-1600000000000-01) é de outra operação e não passa por aqui.
# A regra é "não tem letra nenhuma", não "não tem três letras": se amanhã
# aparecer prefixo de duas ou quatro letras, continua valendo.
RE_PEDIDO_SITE = re.compile(r"\d+-\d+")

# Varredura automática: quantas notas gerar por execução e quantos dias
# para trás considerar na listagem.
PREGERAR_LIMITE = int(os.environ.get("PREGERAR_LIMITE", 20))
PREGERAR_DIAS = int(os.environ.get("PREGERAR_DIAS", 3))
# Orcamento de tempo da varredura, em segundos. Abaixo do teto do cron-job.org (30 s) de
# proposito: melhor devolver resumo parcial em 20 s do que ser cortado sem resposta.
PREGERAR_SEGUNDOS = int(os.environ.get("PREGERAR_SEGUNDOS", 20))

# ---- aparência da DANFE (tudo ajustável por variável de ambiente) ----
# Raio do canto das caixas, em mm. 0 = cantos retos (padrão da lib).
DANFE_RAIO_CANTO = float(os.environ.get("DANFE_RAIO_CANTO", 1.2))
# Multiplicador da fonte de CONTEÚDO dos campos. Rótulos não escalam.
# 1.35 (o FontSize.BIG da lib) transborda a margem direita; 1.18 é o limite
# testado em que nada é cortado.
DANFE_FATOR_FONTE = float(os.environ.get("DANFE_FATOR_FONTE", 1.18))
# Altura da caixa de cada campo, em mm (padrão da lib: 6).
DANFE_ALTURA_CAMPO = float(os.environ.get("DANFE_ALTURA_CAMPO", 7.0))
# Logo: lado máximo em pixels e nº de cores. Na DANFE a logo ocupa ~30mm,
# então acima de ~400px não há ganho visual — só peso no PDF, que o cliente
# baixa no celular. 0 em LOGO_CORES desliga a redução de cores.
LOGO_MAX_PX = int(os.environ.get("LOGO_MAX_PX", 400))
LOGO_CORES = int(os.environ.get("LOGO_CORES", 64))
# Aparar a margem branca em volta da marca. A caixa da logo na DANFE é fixa:
# margem sobrando dentro da imagem vira marca menor na nota. 0 desliga.
LOGO_APARAR = int(os.environ.get("LOGO_APARAR", 1))

# Contato do emitente no cabeçalho. Nenhum dos dois existe no XML da NF-e:
# o fax não é campo do padrão e o e-mail vem do cadastro, não da nota.
# EMIT_FAX vazio repete o telefone (é o que o ERP faz hoje).
EMIT_EMAIL = os.environ.get("EMIT_EMAIL", "")
EMIT_FAX = os.environ.get("EMIT_FAX", "")
# Cabeçalho no formato do ERP: rótulo "Identificação do Emitente", texto à
# esquerda, endereço compacto e linhas de contato. 0 volta ao da biblioteca.
CABECALHO_ERP = int(os.environ.get("CABECALHO_ERP", 1))
# Altura do bloco DADOS ADICIONAIS, em mm (padrão da lib: 20).
# O bloco de produtos ocupa o que sobra da página, então aumentar este
# diminui aquele — é assim que se encolhe a área de produtos.
DANFE_ALTURA_ADICIONAIS = float(os.environ.get("DANFE_ALTURA_ADICIONAIS", 55.0))

# orderIds já resolvidos nesta instância. Evita buscar o mesmo pedido na VTEX
# a cada varredura. Some quando o container reinicia, e isso é inofensivo:
# a execução seguinte apenas confere de novo.
_PREGERADOS = set()

# ---- consulta por e-mail -------------------------------------------------
# Janela de histórico. Pedido mais antigo que isso não entra na lista.
EMAIL_JANELA_DIAS = int(os.environ.get("EMAIL_JANELA_DIAS", 180))
# Quantos pedidos da busca livre podem ser abertos na VTEX para conferir o
# CPF/CNPJ. Cada abertura é uma chamada HTTP: é este número que define o
# tempo de resposta do pior caso.
EMAIL_MAX_ABRIR = int(os.environ.get("EMAIL_MAX_ABRIR", 20))
# Quantas notas devolver na lista de opções para o cliente escolher.
EMAIL_MAX_OPCOES = int(os.environ.get("EMAIL_MAX_OPCOES", 8))
# E-mail que a VTEX devolve mascarado não serve para conferência. Nesses
# casos vale só o CPF/CNPJ, que é conferência exata de qualquer forma.
RE_EMAIL_MASCARADO = re.compile(r"@ct\.vtex\.com\.br$|\.ct\.vtex\.com\.br$", re.I)


def pedido_do_site(order_id: str) -> bool:
    return bool(RE_PEDIDO_SITE.fullmatch((order_id or "").strip()))


# ---------------------------------------------------------------- documento
def so_digitos(valor: Optional[str]) -> str:
    return re.sub(r"\D", "", str(valor or ""))


def _dv_cpf(d: str) -> bool:
    if len(set(d)) == 1:          # 00000000000, 11111111111...
        return False
    for corte in (9, 10):
        soma = sum(int(d[j]) * ((corte + 1) - j) for j in range(corte))
        dv = (soma * 10) % 11 % 10
        if dv != int(d[corte]):
            return False
    return True


def _dv_cnpj(d: str) -> bool:
    if len(set(d)) == 1:
        return False
    pesos1 = [5, 4, 3, 2, 9, 8, 7, 6, 5, 4, 3, 2]
    pesos2 = [6] + pesos1
    for pesos, pos in ((pesos1, 12), (pesos2, 13)):
        soma = sum(int(d[i]) * pesos[i] for i in range(pos))
        dv = 11 - (soma % 11)
        if dv >= 10:
            dv = 0
        if dv != int(d[pos]):
            return False
    return True


def documento_valido(documento: str) -> bool:
    """
    Confere o dígito verificador do CPF/CNPJ.

    Vale a pena conferir antes de chamar a VTEX por dois motivos: um documento
    com DV errado nunca vai bater com nenhum pedido, então a consulta seria
    desperdício; e a mensagem "confira o CPF/CNPJ" é muito mais útil ao
    cliente do que "não encontrei nada".
    """
    d = so_digitos(documento)
    if len(d) == 11:
        return _dv_cpf(d)
    if len(d) == 14:
        return _dv_cnpj(d)
    return False


def _data_vtex(valor: Optional[str]):
    """
    creationDate da VTEX -> datetime com fuso.

    Parser tolerante de propósito: a VTEX manda fração de segundo com 7 casas
    ("2026-08-28T14:32:10.0000000+00:00"), que `fromisoformat` recusa em
    versões mais antigas do Python. Aqui só os campos até o segundo importam,
    porque o uso é um corte de 180 dias.
    """
    if not valor:
        return None
    m = re.match(
        r"(\d{4})-(\d{2})-(\d{2})[T ](\d{2}):(\d{2}):(\d{2})", str(valor).strip()
    )
    if not m:
        return None
    return datetime(*[int(g) for g in m.groups()], tzinfo=timezone.utc)


def _valor_brl(centavos) -> Optional[str]:
    """totalValue da VTEX (em centavos) -> 'R$ 1.234,56'."""
    try:
        v = int(centavos)
    except (TypeError, ValueError):
        return None
    inteiro, dec = "%d" % abs(v // 100), "%02d" % (abs(v) % 100)
    grupos = []
    while len(inteiro) > 3:
        grupos.insert(0, inteiro[-3:])
        inteiro = inteiro[:-3]
    grupos.insert(0, inteiro)
    return "R$ " + ".".join(grupos) + "," + dec


def checar_token(authorization: Optional[str]) -> None:
    esperado = os.environ.get("BRIDGE_TOKEN")
    if not esperado or authorization != f"Bearer {esperado}":
        raise HTTPException(401, "Não autorizado.")

app = FastAPI(title="Bridge DANFE", version="1.0")


# ---------------------------------------------------------------- modelos
class PedidoRequest(BaseModel):
    # Três caminhos de consulta, nesta ordem de precedência:
    #   orderId       -> busca o pedido na VTEX e gera/recupera o PDF
    #   invoiceNumber -> localiza a nota já publicada no storage pela chave
    #   email + documento -> lista as notas daquele cliente para ele escolher
    # Pelo menos um caminho é obrigatório. Com orderId e invoiceNumber juntos,
    # orderId manda e o invoiceNumber só filtra qual nota do pedido.
    orderId: Optional[str] = None
    invoiceNumber: Optional[str] = None
    # desempata quando o mesmo número de nota existe em séries/filiais diferentes
    serie: Optional[str] = None
    # o e-mail é a chave de BUSCA; o documento é a PROVA. Sem documento a
    # consulta por e-mail é recusada — ver buscar_notas_por_email().
    email: Optional[str] = None
    documento: Optional[str] = None


class NotaResponse(BaseModel):
    numero: Optional[str]
    serie: Optional[str]
    chave: str
    pdf_url: str
    emissao: Optional[str] = None   # dd/mm/aaaa, extraída do XML


class OpcaoNota(BaseModel):
    """
    Uma nota que o cliente pode escolher, sem PDF gerado ainda.

    Só metadado de pedido: montar esta lista não custa nenhuma geração de PDF
    nem upload. O PDF sai depois, quando o cliente disser qual nota quer, pelo
    caminho normal do invoiceNumber.
    """
    numero: str
    orderId: str
    data_pedido: Optional[str] = None    # dd/mm/aaaa
    valor: Optional[str] = None          # R$ 1.234,56


class RespostaOk(BaseModel):
    # None quando a consulta foi por número de nota: a chave não carrega o
    # número do pedido, então não há como devolvê-lo por esse caminho.
    orderId: Optional[str] = None
    # vazio quando a resposta é uma lista de opções (consulta por e-mail com
    # mais de uma nota). Nunca vazio junto com `opcoes` vazio: nesse caso a
    # bridge devolve 404 ou 409 em vez de 200.
    notas: List[NotaResponse] = []
    # preenchido só no caminho do e-mail, quando há mais de uma nota
    email: Optional[str] = None
    opcoes: Optional[List[OpcaoNota]] = None


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


def buscar_pedidos_por_nota(numero: str):
    """
    Descobre o pedido a partir do número da nota fiscal.

    A busca livre (?q=) da listagem de pedidos encontra pela nota, e a própria
    resposta traz `invoiceOutput` com os números emitidos — então a
    correspondência é confirmada aqui, sem abrir o pedido. Busca livre pode
    devolver resultado aproximado; sem essa conferência, o risco seria
    entregar a nota de outro cliente.

    Devolve DOIS grupos: (do_site, fora_do_escopo).

    A separação existe porque a Bisturi emite tudo numa única série e numa
    única sequência de numeração: notas do site e notas de marketplace
    (Amazon, Rede, etc.) se intercalam no mesmo intervalo de números, e mais
    de um terço do total é de marketplace. Se o segundo grupo fosse apenas
    descartado, o cliente que comprou em marketplace receberia "confira o
    número" para um número que existe — ele conferiria, digitaria igual, e
    receberia a mesma resposta. Devolvendo os dois grupos, quem chama sabe a
    diferença entre "esse número não existe" e "esse número existe mas é de
    outro canal de venda".
    """
    numero = str(numero).strip().lstrip("0")
    account = os.environ["VTEX_ACCOUNT"]
    env = os.environ.get("VTEX_ENVIRONMENT", "vtexcommercestable")
    resp = requests.get(
        f"https://{account}.{env}.com.br/api/oms/pvt/orders",
        params={"q": numero, "per_page": 15, "page": 1},
        headers={
            "X-VTEX-API-AppKey": os.environ["VTEX_APP_KEY"],
            "X-VTEX-API-AppToken": os.environ["VTEX_APP_TOKEN"],
            "Accept": "application/json",
        },
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        raise HTTPException(502, f"VTEX retornou {resp.status_code} ao buscar a nota.")

    do_site, fora = [], []
    for item in ((resp.json() or {}).get("list")) or []:
        emitidas = [
            str(n).strip().lstrip("0") for n in (item.get("invoiceOutput") or [])
        ]
        order_id = (item.get("orderId") or "").strip()
        if numero not in emitidas:
            continue
        (do_site if pedido_do_site(order_id) else fora).append(order_id)
    return do_site, fora


def buscar_notas_por_email(email: str, documento: str) -> List["OpcaoNota"]:
    """
    Lista as notas fiscais de um cliente a partir do e-mail + CPF/CNPJ.

    O e-mail é a chave de busca, o documento é a prova de identidade. Essa
    separação é o ponto central desta função e não deve ser afrouxada: a busca
    livre (?q=) da VTEX é aproximada e pode trazer pedido de outro cliente, e
    e-mail é um dado que qualquer pessoa pode digitar. Quem autoriza a entrega
    é a conferência do documento contra clientProfileData, feita pedido por
    pedido, aqui embaixo.

    O e-mail também é conferido, mas só quando a VTEX o devolve em claro:
    parte dos pedidos vem com e-mail mascarado (@ct.vtex.com.br), e nesses
    casos a comparação não significaria nada.

    Não gera PDF nenhum: devolve só o metadado das notas para o cliente
    escolher. O PDF sai depois, pelo caminho do invoiceNumber.
    """
    email_norm = (email or "").strip().lower()
    doc = so_digitos(documento)
    account = os.environ["VTEX_ACCOUNT"]
    env = os.environ.get("VTEX_ENVIRONMENT", "vtexcommercestable")
    resp = requests.get(
        f"https://{account}.{env}.com.br/api/oms/pvt/orders",
        params={
            "q": email_norm,
            "f_status": "invoiced",
            "orderBy": "creationDate,desc",
            "per_page": max(EMAIL_MAX_ABRIR, 15),
            "page": 1,
        },
        headers={
            "X-VTEX-API-AppKey": os.environ["VTEX_APP_KEY"],
            "X-VTEX-API-AppToken": os.environ["VTEX_APP_TOKEN"],
            "Accept": "application/json",
        },
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        raise HTTPException(
            502, f"VTEX retornou {resp.status_code} ao buscar pelo e-mail."
        )

    corte = datetime.now(timezone.utc) - timedelta(days=EMAIL_JANELA_DIAS)
    opcoes: List[OpcaoNota] = []
    abertos = 0

    for item in ((resp.json() or {}).get("list")) or []:
        if len(opcoes) >= EMAIL_MAX_OPCOES or abertos >= EMAIL_MAX_ABRIR:
            break

        order_id = (item.get("orderId") or "").strip()
        if not pedido_do_site(order_id):
            continue

        emitidas = [
            str(n).strip().lstrip("0")
            for n in (item.get("invoiceOutput") or [])
            if str(n).strip()
        ]
        if not emitidas:
            continue

        criado = _data_vtex(item.get("creationDate"))
        if criado and criado < corte:
            # a lista vem em ordem decrescente de data: daqui para frente só
            # tem pedido mais antigo ainda
            break

        # ---- conferência de identidade ----
        abertos += 1
        try:
            perfil = (buscar_pedido_vtex(order_id).get("clientProfileData")) or {}
        except HTTPException:
            # um pedido que não abre não invalida a busca inteira
            continue

        documentos = {
            so_digitos(perfil.get("document")),
            so_digitos(perfil.get("corporateDocument")),
        }
        if doc not in documentos:
            continue

        email_pedido = (perfil.get("email") or "").strip().lower()
        if (
            email_pedido
            and not RE_EMAIL_MASCARADO.search(email_pedido)
            and email_pedido != email_norm
        ):
            continue

        data_br = criado.strftime("%d/%m/%Y") if criado else None
        for numero in emitidas:
            opcoes.append(
                OpcaoNota(
                    numero=numero,
                    orderId=order_id,
                    data_pedido=data_br,
                    valor=_valor_brl(item.get("totalValue")),
                )
            )

    return opcoes[:EMAIL_MAX_OPCOES]


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
        # dhEmi é o campo atual (datetime ISO); dEmi é o legado (só data)
        "emissao": formatar_data(txt("dhEmi", ide) or txt("dEmi", ide)),
    }


def formatar_data(valor: Optional[str]) -> Optional[str]:
    """
    '2026-08-28T15:19:00-03:00' -> '28/08/2026'. Devolve o valor original se
    não reconhecer o formato: melhor uma data estranha que nenhuma.
    """
    if not valor:
        return None
    try:
        return date.fromisoformat(valor[:10]).strftime("%d/%m/%Y")
    except ValueError:
        return valor


# ---------------------------------------------------------------- PDF
_LOGO_CACHE: dict = {}


def preparar_logo(caminho: str):
    """
    Reduz a logo antes de embutir no PDF, e guarda o resultado em memória.

    Sem isso, uma logo grande vira peso em TODA nota: uma imagem de
    4000x4000 embutida faz cada DANFE passar de 1MB, e é o cliente que
    baixa isso no celular. Na DANFE a logo ocupa cerca de 30mm — acima de
    LOGO_MAX_PX não existe ganho visual.

    Falha aqui nunca derruba a geração: qualquer problema devolve o caminho
    original e o PDF sai com a logo grande. É otimização, não requisito.
    """
    chave = (caminho, os.path.getmtime(caminho), LOGO_MAX_PX, LOGO_CORES)
    if chave in _LOGO_CACHE:
        return io.BytesIO(_LOGO_CACHE[chave])

    try:
        from PIL import Image

        im = Image.open(caminho)
        # achata transparência sobre branco: evita máscara alfa no PDF
        if im.mode in ("RGBA", "LA", "P"):
            fundo = Image.new("RGB", im.size, "white")
            im = im.convert("RGBA")
            fundo.paste(im, mask=im.split()[-1])
            im = fundo
        else:
            im = im.convert("RGB")

        if LOGO_APARAR:
            from PIL import ImageChops

            branco = Image.new("RGB", im.size, "white")
            caixa = ImageChops.difference(im, branco).getbbox()
            # caixa None = imagem toda branca; ignora nesse caso
            if caixa and (caixa[2] - caixa[0]) > 1 and (caixa[3] - caixa[1]) > 1:
                im = im.crop(caixa)

        if max(im.size) > LOGO_MAX_PX:
            fator = LOGO_MAX_PX / max(im.size)
            im = im.resize(
                (max(1, int(im.width * fator)), max(1, int(im.height * fator))),
                Image.LANCZOS,
            )
        if LOGO_CORES > 0:
            im = im.quantize(colors=LOGO_CORES, method=Image.MEDIANCUT)

        buf = io.BytesIO()
        im.save(buf, format="PNG", optimize=True)
        _LOGO_CACHE.clear()          # só a versão corrente interessa
        _LOGO_CACHE[chave] = buf.getvalue()
        return io.BytesIO(_LOGO_CACHE[chave])
    except Exception:
        return caminho


_CABECALHO_APLICADO = False


def aplicar_cabecalho_erp() -> None:
    """
    Reescreve a caixa do emitente no formato do ERP da Bisturi.

    A biblioteca desenha essa caixa em duas classes pequenas e isoladas
    (DanfeEmitInfo e DanfeIdentInfo), instanciadas por nome dentro de
    _draw_header. Substituindo as classes no módulo, herdamos os 112 linhas
    de geometria do cabeçalho sem copiá-las — só o conteúdo das duas células
    é reescrito.

    Diferenças em relação ao padrão da lib:
      - rótulo "Identificação do Emitente" no topo da caixa
      - texto alinhado à esquerda, não centralizado
      - endereço em duas linhas compactas, não cinco centralizadas
      - linhas de Telefone, Fax e E-mail
      - número da nota sem zeros à esquerda ("Nº 371006", não "Nº 000.371.006")
    """
    global _CABECALHO_APLICADO
    if _CABECALHO_APLICADO:
        return

    from brazilfiscalreport.danfe import danfe as _mod
    from brazilfiscalreport.danfe.danfe_emit_info import DanfeEmitInfo
    from brazilfiscalreport.danfe.danfe_ident_info import DanfeIdentInfo
    from brazilfiscalreport.utils import format_phone

    class EmitInfoBisturi(DanfeEmitInfo):
        def render(self):
            # a moldura da célula vem do Element; aqui só o conteúdo
            Element_render = super(DanfeEmitInfo, self).render
            Element_render()

            pdf = self.pdf
            emit = getattr(pdf, "emit", None)

            def campo(tag):
                # extract_text vive no módulo danfe, não em utils
                return _mod.extract_text(emit, tag) if emit is not None else ""

            fone = format_phone(campo("fone"))
            fax = EMIT_FAX or fone
            email = EMIT_EMAIL or campo("email")

            w_logo, h_logo = 30, 18
            x_texto = self.x + w_logo + 3
            w_texto = self.w - w_logo - 4

            # rótulo do bloco, centralizado sobre a área de texto
            pdf.set_font(pdf.default_font, "", 6.5)
            pdf.set_xy(x=x_texto, y=self.y + 0.8)
            pdf.cell(w=w_texto, h=3, text="Identificação do Emitente", align="C")

            # logo centralizada verticalmente na caixa
            if self.logo_image:
                pdf.image(
                    name=self.logo_image,
                    x=self.x + 2,
                    y=self.y + (self.h - h_logo) / 2,
                    w=w_logo,
                    h=h_logo,
                    keep_aspect_ratio=True,
                )

            from brazilfiscalreport.utils import format_cep

            linhas = [
                self.emit,
                f"{campo('xLgr')} - {campo('nro')} -",
                f"{campo('xBairro')} - {campo('xMun')} - "
                f"{campo('UF')} - {format_cep(campo('CEP'))}",
                "",
            ]
            rotulados = [("Telefone:", fone), ("Fax:", fax), ("E-mail:", email)]

            def escrever(x, y, largura, texto, tamanho=7.0):
                """
                cell() do fpdf não corta texto: o que não cabe invade a
                célula vizinha. A razão social é longa, então a fonte
                encolhe até caber, com piso de 5pt.
                """
                while tamanho > 5.0:
                    pdf.set_font(pdf.default_font, "", tamanho)
                    if pdf.get_string_width(texto) <= largura:
                        break
                    tamanho -= 0.25
                pdf.set_xy(x=x, y=y)
                pdf.cell(w=largura, h=3, text=texto, align="L")

            y = self.y + 4.2
            for linha in linhas:
                escrever(x_texto, y, w_texto, linha)
                y += 3

            for rotulo, valor in rotulados:
                if not valor:
                    continue
                pdf.set_font(pdf.default_font, "B", 7)
                pdf.set_xy(x=x_texto, y=y)
                pdf.cell(w=13, h=3, text=rotulo, align="L")
                escrever(x_texto + 13, y, w_texto - 13, str(valor))
                y += 3

    class IdentInfoBisturi(DanfeIdentInfo):
        """
        Igual à da lib, com uma diferença: o número da nota sai puro
        ("Nº 371006") em vez de preenchido com zeros ("Nº 000.371.006").
        A lib formata com int(nr_nota):011 e não expõe gancho para isso,
        então este render é a cópia dela com essa linha alterada.
        """

        def render(self):
            super(DanfeIdentInfo, self).render()
            pdf = self.pdf
            pdf.set_xy(x=self.x, y=self.y)
            pdf.set_font(pdf.default_font, "B", 12)
            pdf.cell(self.w, None, "DANFE", new_x="LEFT", new_y="NEXT", align="C")
            pdf.set_font(pdf.default_font, "", 7)
            for txt in ("DOCUMENTO AUXILIAR", "DA NOTA FISCAL", "ELETRÔNICA"):
                pdf.cell(self.w, None, txt, new_x="LEFT", new_y="NEXT", align="C")

            pdf.set_font(pdf.default_font, "", 8)
            pos_x = pdf.get_x() + 1
            pos_y = pdf.get_y() + 1
            pdf.set_xy(x=pos_x, y=pos_y)
            pdf.cell(self.w, 3, "0-ENTRADA", new_x="LEFT", new_y="NEXT", align="L")
            pdf.cell(self.w, 3, "1-SAÍDA", new_x="LEFT", new_y="NEXT", align="L")
            pos_x2 = pdf.get_x()
            pos_y2 = pdf.get_y() + 0.5

            pdf.set_font(pdf.default_font, "B", 10)
            pdf.text_box(
                text=self.tp_nf, text_align="C", h_line=4,
                x=pos_x + 25, y=pos_y, w=5, h=5, border=1,
            )

            pdf.set_font(pdf.default_font, "B", 10)
            pdf.set_xy(x=pos_x2, y=pos_y2)
            # >>> a única mudança em relação à lib <<<
            pdf.cell(
                self.w, 5, f"Nº {int(self.nr_nota)}",
                new_x="LEFT", new_y="NEXT", align="L",
            )
            pdf.set_font(pdf.default_font, "B", 8)
            pdf.cell(
                self.w, None, f"SÉRIE {self.serie_nf}",
                new_x="LEFT", new_y="NEXT", align="L",
            )
            pdf.cell(self.w, None, f"FOLHA {pdf.page_no()}/{{nb}}", align="L")

    _mod.DanfeEmitInfo = EmitInfoBisturi
    _mod.DanfeIdentInfo = IdentInfoBisturi
    _CABECALHO_APLICADO = True


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
        config.logo = preparar_logo(logo)

    # A altura do campo é lida como constante de módulo pela lib, então
    # precisa ser trocada nos dois lugares onde ela foi importada.
    from brazilfiscalreport.danfe import danfe_basic_field, danfe_conf
    from brazilfiscalreport.danfe.danfe_block import DanfeBlock
    from brazilfiscalreport.danfe.danfe_conf import (
        BASE_FONT_SIZES,
        HEIGHT_FONT_BLOCK_DESC,
    )
    from brazilfiscalreport.danfe.models import BaseFieldInfo

    danfe_conf.DEFAULT_FIELD_HEIGHT = DANFE_ALTURA_CAMPO
    danfe_basic_field.DEFAULT_FIELD_HEIGHT = DANFE_ALTURA_CAMPO

    class DanfeBisturi(Danfe):
        """
        Layout customizado. Cada override existe por um motivo específico:

        rect()            canto arredondado em TODOS os campos de uma vez —
                          toda caixa da DANFE passa por Element.render(),
                          que chama pdf.rect(). Um ponto, efeito global.
        get_font_size()   fonte de conteúdo maior sem mexer nos rótulos.
                          A tabela de produtos fica fora da escala: as
                          colunas NCM e UN são estreitas e o texto quebraria
                          dentro da célula.
        _get_additional…  quebra de linha depois de cada "//" das informações
                          complementares. O "//" é preservado — nada se
                          perde do texto original, só ganha organização.
        _draw_additional… altura do bloco DADOS ADICIONAIS configurável
                          (a lib fixa 20mm no código).

        Os overrides dependem de nomes internos da brazilfiscalreport, que
        está fixada em 1.0.2 no requirements.txt. Ao atualizar a lib, gere
        um PDF de teste antes de subir.
        """

        SEM_ESCALA_DE_FONTE = {"PRODUCT_DESCRIPTION"}

        def rect(self, x, y, w, h, style=None, round_corners=False, corner_radius=0):
            if DANFE_RAIO_CANTO <= 0:
                return super().rect(x, y, w, h, style=style)
            return super().rect(
                x, y, w, h,
                style=style,
                round_corners=True,
                corner_radius=DANFE_RAIO_CANTO,
            )

        def get_font_size(self, element_type: str, multiplier=False):
            base = BASE_FONT_SIZES.get(element_type)
            if not multiplier or element_type in self.SEM_ESCALA_DE_FONTE:
                return base
            return base * DANFE_FATOR_FONTE

        def _get_additional_data_content(self):
            texto = super()._get_additional_data_content() or ""
            return texto.replace("//", "//\n")

        def _draw_additional_data(self, additional_data, continuation_height=None):
            bloco = DanfeBlock(description="DADOS ADICIONAIS", pdf=self)
            altura = (
                continuation_height - HEIGHT_FONT_BLOCK_DESC
                if continuation_height
                else DANFE_ALTURA_ADICIONAIS
            )
            bloco.rows_heights = (altura,)
            if not continuation_height:
                campos = [
                    BaseFieldInfo(
                        w=0,
                        description="INFORMAÇÕES COMPLEMENTARES",
                        content=additional_data,
                        type="info_complementares",
                    ),
                    BaseFieldInfo(w=70, description="RESERVADO AO FISCO", content=""),
                ]
            else:
                campos = [
                    BaseFieldInfo(
                        w=0,
                        description="CONTINUAÇÃO INFORMAÇÕES COMPLEMENTARES",
                        content=additional_data,
                    ),
                ]
            bloco.add_fields([campos])
            bloco.render()
            campo = bloco.fields[0]
            return campo.get_content_lines(), campo.get_max_content_lines()

    if CABECALHO_ERP:
        aplicar_cabecalho_erp()

    # preserva os "\n" que injetamos nas informações complementares
    config.infcpl_semicolon_newline = True

    pdf = bytes(DanfeBisturi(xml=xml_content, config=config).output())
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
    """Conecta no SFTP COM PRAZO.

    Sem prazo, `paramiko.Transport((host, porta))` espera o timeout do sistema
    operacional — uns dois minutos — antes de devolver `[Errno 110] Connection timed
    out`. Nesse tempo o proxy do Render desiste e o cliente recebe 502 ou "conexao
    fechada", sem nenhum erro util. Medido em 03/09/2026: /danfe estourando aqui,
    /pregerar devolvendo 502 e /expurgo pendurado, todos pela mesma causa.

    Com prazo, a falha aparece em segundos e diz o que e. SFTP_TIMEOUT ajusta.
    """
    import socket

    import paramiko

    prazo = float(os.environ.get("SFTP_TIMEOUT", 15))
    endereco = (os.environ["SFTP_HOST"], int(os.environ.get("SFTP_PORT", 22)))
    sock = socket.create_connection(endereco, timeout=prazo)
    transport = paramiko.Transport(sock)
    transport.banner_timeout = prazo
    transport.auth_timeout = prazo
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


def _ftps_existe(chave: str, ftps=None) -> bool:
    # ftps != None: reaproveita a conexão do lote (usado pela varredura)
    proprio = ftps is None
    if proprio:
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
        if proprio:
            _ftps_fechar(ftps)


def _ftps_subir(pdf_bytes: bytes, chave: str, ftps=None) -> str:
    proprio = ftps is None
    if proprio:
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
        if proprio:
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
def ja_existe(chave: str, conn=None) -> bool:
    if STORAGE_BACKEND == "ftps":
        return _ftps_existe(chave, conn)
    return _sftp_existe(chave) if STORAGE_BACKEND == "sftp" else _s3_existe(chave)


def url_publica(chave: str) -> str:
    if STORAGE_BACKEND in ("ftps", "sftp"):
        return _sftp_public_url(chave)
    return _s3_url(chave)


def subir_pdf(pdf_bytes: bytes, chave: str, conn=None) -> str:
    if STORAGE_BACKEND == "ftps":
        return _ftps_subir(pdf_bytes, chave, conn)
    return _sftp_subir(pdf_bytes, chave) if STORAGE_BACKEND == "sftp" else _s3_subir(pdf_bytes, chave)


# ---- consulta por número de nota -------------------------------------------
# A chave de acesso da NF-e tem posições fixas (44 dígitos):
#   [0:2] cUF | [2:6] AAMM | [6:20] CNPJ | [20:22] modelo
#   [22:25] série | [25:34] número da nota | [34] tpEmis | [35:43] cNF | [43] DV
# Como o nome do arquivo no storage É a chave, o número da nota pode ser lido
# direto do nome — sem nenhuma chamada à VTEX. Conferido contra chaves reais.
def nnf_da_chave(chave: str) -> str:
    return chave[25:34].lstrip("0")


def serie_da_chave(chave: str) -> str:
    return chave[22:25].lstrip("0")


def _ftps_listar(conn=None) -> List[str]:
    ftps = conn or _ftps()
    try:
        pasta = os.environ.get("SFTP_BASE_DIR", "/public/danfe").rstrip("/")
        try:
            _ftps_cd(ftps, pasta)
        except error_perm:
            return []
        return ftps.nlst()
    finally:
        if conn is None:
            _ftps_fechar(ftps)


def listar_chaves(conn=None) -> List[str]:
    """Chaves das notas que já têm PDF publicado."""
    if STORAGE_BACKEND == "ftps":
        nomes = _ftps_listar(conn)
    elif STORAGE_BACKEND == "sftp":
        sftp, transport = _sftp()
        try:
            base = os.environ.get("SFTP_BASE_DIR", "/public/danfe").rstrip("/")
            nomes = sftp.listdir(base)
        finally:
            sftp.close()
            transport.close()
    else:
        resp = _s3().list_objects_v2(
            Bucket=os.environ["R2_BUCKET"], Prefix="danfe/"
        )
        nomes = [o["Key"] for o in resp.get("Contents", [])]

    chaves = []
    for nome in nomes:
        base = nome.rsplit("/", 1)[-1]
        if base.lower().endswith(".pdf"):
            chave = base[:-4]
            if re.fullmatch(r"\d{44}", chave):
                chaves.append(chave)
    return chaves


def gerar_notas_do_pedido(
    order_id: str, invoice_number: Optional[str] = None, conn=None
) -> List["NotaResponse"]:
    """
    Pedido -> lista de NotaResponse, gerando o PDF só do que ainda não existe.
    Usado pelos dois caminhos de consulta (por pedido e por número de nota).

    A origem do XML sai do formato do número: pedido do site tem hífen e vem da
    VTEX; pedido do ERP é só dígitos e vem do WinThor. Daqui para baixo o
    tratamento é idêntico, então nota do ERP sai no mesmo formato de entrega.
    """
    if pedido_do_winthor(order_id):
        encontrados = extrair_xmls_winthor(order_id, invoice_number)
    else:
        encontrados = extrair_xmls(buscar_pedido_vtex(order_id), invoice_number)
    notas = []
    for item in encontrados:
        info = validar_xml(item["xml"])
        chave = info["chave"]
        if ja_existe(chave, conn):
            url = url_publica(chave)   # cache: não regenera o PDF
        else:
            url = subir_pdf(gerar_pdf(item["xml"]), chave, conn)
        notas.append(
            NotaResponse(
                numero=info["numero"],
                serie=info["serie"],
                chave=chave,
                pdf_url=url,
                emissao=info.get("emissao"),
            )
        )
    return notas


def buscar_por_numero(numero: str, serie: Optional[str] = None, conn=None) -> List[str]:
    numero = str(numero).strip().lstrip("0")
    achadas = [c for c in listar_chaves(conn) if nnf_da_chave(c) == numero]
    if serie:
        s = str(serie).strip().lstrip("0")
        achadas = [c for c in achadas if serie_da_chave(c) == s]
    return sorted(achadas)


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


def resolver_uma_nota(
    numero: str, order_id: str, serie: Optional[str] = None
) -> List["NotaResponse"]:
    """
    Número da nota + pedido de origem -> NotaResponse pronta.

    Tenta primeiro o acervo publicado (custo: uma listagem de pasta) e só
    recorre à VTEX se o PDF ainda não existir. Mesma ordem do caminho do
    invoiceNumber, para o comportamento ser idêntico pelos dois caminhos.
    """
    chaves = buscar_por_numero(numero, serie)
    if chaves:
        return [
            NotaResponse(
                numero=nnf_da_chave(c),
                serie=serie_da_chave(c),
                chave=c,
                pdf_url=url_publica(c),
            )
            for c in chaves
        ]
    return gerar_notas_do_pedido(order_id, numero)


# ---------------------------------------------------------------- endpoint
@app.post("/danfe", response_model=RespostaOk)
def danfe(req: PedidoRequest, authorization: str = Header(None)):
    checar_token(authorization)

    # ---- caminho 2: cliente informou o número da nota, não o do pedido ----
    if not req.orderId:
        # ---- caminho 3: nem nota nem pedido, só e-mail + CPF/CNPJ ----
        # Vem antes do 422 e depois do invoiceNumber: se o cliente já informou
        # o número da nota, ele é mais específico e não há por que listar.
        if not req.invoiceNumber and req.email:
            if not req.documento:
                # 422 aqui é proposital: é falta de parâmetro, não "não achei".
                raise HTTPException(
                    422, "Consulta por e-mail exige também o CPF/CNPJ do cadastro."
                )
            if not documento_valido(req.documento):
                raise HTTPException(
                    422, "CPF/CNPJ inválido (dígito verificador não confere)."
                )

            opcoes = buscar_notas_por_email(req.email, req.documento)
            if not opcoes:
                raise HTTPException(
                    404,
                    "Nenhuma nota fiscal localizada para esse e-mail e CPF/CNPJ "
                    f"nos últimos {EMAIL_JANELA_DIAS} dias.",
                )

            # uma só nota: não faz sentido perguntar qual. Entrega direto.
            if len(opcoes) == 1:
                unica = opcoes[0]
                notas = resolver_uma_nota(unica.numero, unica.orderId)
                if not notas:
                    raise HTTPException(
                        409,
                        "Nota localizada no pedido, mas sem XML disponível ainda.",
                    )
                return RespostaOk(orderId=unica.orderId, notas=notas)

            # várias: devolve a lista para o cliente escolher. Nenhum PDF é
            # gerado aqui — o escolhido vem depois, por invoiceNumber.
            return RespostaOk(email=req.email.strip(), opcoes=opcoes)

        if not req.invoiceNumber:
            raise HTTPException(
                422, "Informe orderId, invoiceNumber ou email + documento."
            )

        # 2a. já publicada? resolve pelo nome do arquivo, sem tocar na VTEX
        chaves = buscar_por_numero(req.invoiceNumber, req.serie)
        if chaves:
            return RespostaOk(
                orderId=None,
                notas=[
                    NotaResponse(
                        numero=nnf_da_chave(c),
                        serie=serie_da_chave(c),
                        chave=c,
                        pdf_url=url_publica(c),
                    )
                    for c in chaves
                ],
            )

        # 2b. não publicada: descobre o pedido pela nota e gera na hora.
        # O cliente não precisa saber que existem dois caminhos.
        pedidos, fora_do_escopo = buscar_pedidos_por_nota(req.invoiceNumber)
        if not pedidos and fora_do_escopo:
            # A nota EXISTE, mas pertence a pedido de marketplace. Mesmo 400 do
            # caminho do orderId, para o agente cair na mesma regra: devolver ao
            # fluxo principal em vez de mandar o cliente conferir um número que
            # está certo.
            raise HTTPException(
                400,
                "Nota %s pertence ao pedido %s, que não é do site."
                % (req.invoiceNumber, fora_do_escopo[0]),
            )
        if not pedidos:
            raise HTTPException(
                404,
                f"Nota {req.invoiceNumber} não localizada. Confira o número.",
            )

        notas = []
        for order_id in pedidos:
            notas.extend(
                gerar_notas_do_pedido(order_id, req.invoiceNumber)
            )
        if not notas:
            raise HTTPException(
                409, "Nota localizada no pedido, mas sem XML disponível ainda."
            )
        return RespostaOk(
            orderId=pedidos[0] if len(pedidos) == 1 else None, notas=notas
        )

    if not pedido_do_site(req.orderId) and not pedido_do_winthor(req.orderId):
        # 400: pedido de outra operação (prefixo de letras: marketplace, PGM).
        # Não é erro técnico nem "nota não emitida" — o agente deve devolver ao
        # manager, que já tem regra própria para esses pedidos.
        #
        # Pedido do ERP (só dígitos) NÃO cai mais aqui: tem fonte própria de
        # XML no WinThor e segue pelo gerar_notas_do_pedido.
        raise HTTPException(
            400,
            "Pedido fora do escopo desta consulta (não é pedido do site).",
        )

    notas = gerar_notas_do_pedido(req.orderId, req.invoiceNumber)

    if not notas:
        # 409: pedido existe mas ainda não tem nota com XML. O agente deve
        # responder "sua nota ainda não foi emitida", não "erro".
        raise HTTPException(
            409, "Pedido sem nota fiscal disponível (ainda não emitida)."
        )

    return RespostaOk(orderId=req.orderId, notas=notas)


def listar_pedidos_faturados(por_pagina: int = 50) -> List[dict]:
    """
    Lista os pedidos faturados mais recentes na VTEX.

    Sem filtro de data na query de propósito: menos sintaxe da API de Orders
    para errar. O corte por data é feito aqui no código, sobre creationDate.
    """
    account = os.environ["VTEX_ACCOUNT"]
    env = os.environ.get("VTEX_ENVIRONMENT", "vtexcommercestable")
    resp = requests.get(
        f"https://{account}.{env}.com.br/api/oms/pvt/orders",
        params={
            "f_status": "invoiced",
            "orderBy": "creationDate,desc",
            "per_page": por_pagina,
            "page": 1,
        },
        headers={
            "X-VTEX-API-AppKey": os.environ["VTEX_APP_KEY"],
            "X-VTEX-API-AppToken": os.environ["VTEX_APP_TOKEN"],
            "Accept": "application/json",
        },
        timeout=TIMEOUT,
    )
    if resp.status_code != 200:
        raise HTTPException(502, f"VTEX retornou {resp.status_code} ao listar pedidos.")
    return ((resp.json() or {}).get("list")) or []


@app.api_route("/pregerar", methods=["GET", "POST"])
def pregerar(
    limite: Optional[int] = None,
    dias: Optional[int] = None,
    segundos: Optional[int] = None,
    authorization: str = Header(None),
):
    """
    Varredura: gera no storage o DANFE das notas faturadas que ainda não têm PDF.

    Idempotente — o que já existe é apenas contado. Pode rodar quantas vezes
    quiser; se uma execução falhar, a próxima conserta.

    Chamada por cron (cron-job.org). Serve também de keep-alive: mantém a
    instância acordada fazendo trabalho útil em vez de bater num /health vazio.

    **Orçamento de tempo** (`segundos`, default PREGERAR_SEGUNDOS = 20): a varredura
    para quando o tempo acaba e devolve o que conseguiu, em vez de tentar o lote
    inteiro. Medido em 03/09/2026: com o upload finalmente funcionando, `limite=5`
    passou dos 30 s e o cron-job.org cortou com "Failed (timeout)" — e um lote cortado
    no meio pela rede nem devolve o resumo, então nao se sabe o que foi feito.
    Sendo idempotente, parar cedo nao perde nada: a proxima execucao continua.
    """
    checar_token(authorization)

    limite = PREGERAR_LIMITE if limite is None else max(1, limite)
    dias = PREGERAR_DIAS if dias is None else max(1, dias)
    orcamento = PREGERAR_SEGUNDOS if segundos is None else max(5, segundos)
    corte = date.today() - timedelta(days=dias)
    comeco = time.monotonic()

    resumo = {
        "geradas": [],
        "ja_existiam": 0,
        "sem_nota": 0,
        "fora_do_escopo": 0,
        "consultados": 0,
        "erros": [],
        "tempo_esgotado": False,
    }

    # uma conexão FTPS para o lote inteiro, em vez de uma por arquivo
    conn = _ftps() if STORAGE_BACKEND == "ftps" else None
    try:
        for item in listar_pedidos_faturados():
            if len(resumo["geradas"]) >= limite:
                break
            # Checa o orcamento ANTES de comecar mais um pedido: parar entre pedidos
            # deixa o resumo coerente, parar no meio de um upload deixaria .part na pasta.
            if time.monotonic() - comeco > orcamento:
                resumo["tempo_esgotado"] = True
                break

            order_id = (item.get("orderId") or "").strip()
            if not order_id or order_id in _PREGERADOS:
                continue

            criado = (item.get("creationDate") or "")[:10]
            try:
                if criado and date.fromisoformat(criado) < corte:
                    continue
            except ValueError:
                pass

            if not pedido_do_site(order_id):
                resumo["fora_do_escopo"] += 1
                _PREGERADOS.add(order_id)
                continue

            resumo["consultados"] += 1
            try:
                achados = extrair_xmls(buscar_pedido_vtex(order_id))
                if not achados:
                    # nota pode sair depois: não marca como resolvido
                    resumo["sem_nota"] += 1
                    continue

                for it in achados:
                    info = validar_xml(it["xml"])
                    chave = info["chave"]
                    if ja_existe(chave, conn):
                        resumo["ja_existiam"] += 1
                        continue
                    subir_pdf(gerar_pdf(it["xml"]), chave, conn)
                    resumo["geradas"].append(
                        {"pedido": order_id, "nota": info["numero"], "chave": chave}
                    )

                _PREGERADOS.add(order_id)

            except HTTPException as e:
                # um pedido problemático não pode abortar o lote
                resumo["erros"].append(
                    {"pedido": order_id, "erro": f"{e.status_code}: {e.detail}"}
                )
            except Exception as e:
                resumo["erros"].append({"pedido": order_id, "erro": type(e).__name__})
    finally:
        if conn is not None:
            _ftps_fechar(conn)

    resumo["memoria"] = len(_PREGERADOS)
    resumo["segundos"] = round(time.monotonic() - comeco, 1)
    return resumo


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/expurgo")
def expurgo(dias: int = 7, authorization: str = Header(None)):
    """Apaga DANFEs com mais de `dias` dias. Feito para ser chamado por cron.

    O README lista "expurgar_antigos(7) por cron, diario" como a mitigacao 3 do risco de
    link permanente, com status "agendar" — mas nao havia rota nenhuma que chegasse na
    funcao. As rotas eram /danfe, /pregerar e /health. Ou seja: a mitigacao nao estava
    apenas nao agendada, estava inalcancavel. Achado e corrigido em 03/09/2026.

    Nada muda por si: sem alguem chamar esta rota, o comportamento e o de antes.
    """
    checar_token(authorization)
    return {"apagados": expurgar_antigos(dias), "dias": dias}


# ---------------------------------------------------------------- cotacao em PDF
# Rotas POST /cotacao e GET /cotacao/{numero}, em modulo proprio (cotacao_api.py) para
# nao mexer nas funcoes de storage deste arquivo, que estao em producao servindo DANFE.
# O modulo grava em outra pasta (COTACAO_BASE_DIR) e nao importa nada daqui.
from cotacao_api import router as cotacao_router  # noqa: E402

app.include_router(cotacao_router)
