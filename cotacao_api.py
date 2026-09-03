"""Rotas da cotacao em PDF, acopladas a bridge existente.

Por que modulo separado, e nao alteracao das funcoes de storage do bridge_danfe:
acrescentar um parametro de pasta em `_sftp_remote_path`, `_sftp_public_url`,
`subir_pdf` e `expurgar_antigos` mexeria em quatro funcoes de um servico de nota fiscal
que esta em producao, para atender um recurso que grava em OUTRA pasta de qualquer forma.
O modulo carrega as proprias tres funcoes de SFTP, le as mesmas variaveis de ambiente e
nao importa nada do bridge_danfe — o que tambem evita import circular, ja que e o
bridge_danfe que inclui este router. Duplicar helper pequeno e a convencao da casa
(as tools do repositorio copiam `search_products` inteiro entre pastas).

Para ligar, duas linhas no fim do bridge_danfe.py:

    from cotacao_api import router as cotacao_router
    app.include_router(cotacao_router)

Rotas:
    POST /cotacao            -> gera (ou reaproveita) o PDF e devolve a URL publica
    GET  /cotacao/{numero}   -> acha pelo numero, sem regerar; diz se venceu

Nome do arquivo: "{numero}-{token}.pdf". O numero na frente permite achar pelo numero
listando a pasta; o token de 16 caracteres impede que alguem chegue na cotacao de outro
cliente somando 1 ao numero. So o numero como nome seria adivinhavel — e o PDF carrega
nome, endereco, CPF e IE. E o mesmo motivo pelo qual o README manda apagar o
"371006.pdf" de teste da pasta do DANFE.
"""

import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Header, HTTPException, Query
from pydantic import BaseModel, Field

from cotacao_pdf import gerar_pdf

router = APIRouter()

BASE_DIR = os.environ.get("COTACAO_BASE_DIR", "/public/cotacao").rstrip("/")
PUBLIC_BASE_URL = os.environ.get(
    "COTACAO_PUBLIC_BASE_URL", "https://arquivos.bisturi.com.br/cotacao"
).rstrip("/")

# O documento imprime "Cotacao valida por 24 horas". Passado esse prazo o arquivo
# continua no servidor (por decisao de 03/09 nao ha expurgo), mas a API para de
# entrega-lo como valido: devolver preco vencido contradiz o que esta escrito no papel.
VALIDADE_HORAS = float(os.environ.get("COTACAO_VALIDADE_HORAS", 24))

# Numero: letras, digitos e hifen, em blocos. Nada de barra, ponto ou espaco — ele entra
# em nome de arquivo e em comparacao de prefixo, e ".." aqui seria travessia de caminho.
RE_NUMERO = re.compile(r"^[A-Za-z0-9]{2,12}(?:-[A-Za-z0-9]{2,12}){0,2}$")

FUSO = timezone(timedelta(hours=-3))


def checar_token(authorization: Optional[str]) -> None:
    """Mesma chave do DANFE. Repetida aqui de proposito, para nao importar o bridge."""
    esperado = os.environ.get("BRIDGE_TOKEN")
    if not esperado or authorization != f"Bearer {esperado}":
        raise HTTPException(401, "Nao autorizado.")


def agora():
    try:
        from zoneinfo import ZoneInfo

        return datetime.now(ZoneInfo("America/Sao_Paulo"))
    except Exception:
        # Container sem base de fusos: -03:00 fixo. Erra no horario de verao, que
        # nao existe no Brasil desde 2019.
        return datetime.now(FUSO)


def gerar_numero() -> str:
    """Numero curto, falavel ao telefone e nao sequencial: COT-0309-7K2M.

    Sequencial seria pior mesmo com o token no nome: o cliente que ouve "sua cotacao e
    a 9217" aprende quantas cotacoes a loja fez. Data mais quatro caracteres resolve.
    """
    alfabeto = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # sem O/0 e I/1, que confundem na fala
    sufixo = "".join(secrets.choice(alfabeto) for _ in range(4))
    return f"COT-{agora():%d%m}-{sufixo}"


# ------------------------------------------------------------------ storage (SFTP)


def _sftp():
    import paramiko

    transport = paramiko.Transport(
        (os.environ["SFTP_HOST"], int(os.environ.get("SFTP_PORT", 22)))
    )
    chave = os.environ.get("SFTP_KEY_PATH")
    if chave:
        transport.connect(
            username=os.environ["SFTP_USER"],
            pkey=paramiko.RSAKey.from_private_key_file(chave),
        )
    else:
        transport.connect(
            username=os.environ["SFTP_USER"], password=os.environ["SFTP_PASSWORD"]
        )
    return paramiko.SFTPClient.from_transport(transport), transport


def _garantir_pasta(sftp) -> None:
    try:
        sftp.stat(BASE_DIR)
    except IOError:
        sftp.mkdir(BASE_DIR)


def subir(pdf_bytes: bytes, nome: str) -> str:
    """Grava com nome temporario e renomeia, como o DANFE faz.

    Sem isso, quem abrir a URL no exato instante do upload baixa um PDF pela metade.
    """
    sftp, transport = _sftp()
    try:
        _garantir_pasta(sftp)
        destino = f"{BASE_DIR}/{nome}"
        parcial = destino + ".part"
        with sftp.file(parcial, "wb") as arquivo:
            arquivo.write(pdf_bytes)
        try:
            sftp.remove(destino)
        except IOError:
            pass
        sftp.rename(parcial, destino)
        return f"{PUBLIC_BASE_URL}/{nome}"
    finally:
        sftp.close()
        transport.close()


def procurar(numero: str) -> Optional[dict]:
    """Acha o PDF de um numero listando a pasta e casando o prefixo.

    Mesma estrategia do `buscar_por_numero` do DANFE, que casa posicoes dentro da chave
    da NF-e sobre `listar_chaves()`. Nao ha indice: o nome do arquivo E o indice.

    Custo: uma listagem da pasta por consulta. Como nao ha expurgo, a pasta cresce sem
    limite; a listagem aguenta bem dezenas de milhares de nomes, e se um dia doer o
    conserto e guardar por mes (`/public/cotacao/2609/...`), derivando a subpasta do
    proprio numero.
    """
    prefixo = f"{numero}-"
    sftp, transport = _sftp()
    try:
        try:
            entradas = sftp.listdir_attr(BASE_DIR)
        except IOError:
            return None
        achados = [
            e for e in entradas
            if e.filename.startswith(prefixo) and e.filename.lower().endswith(".pdf")
        ]
        if not achados:
            return None
        # Mais de um arquivo para o mesmo numero so acontece se alguem regerou; vale o
        # mais novo.
        recente = max(achados, key=lambda e: e.st_mtime or 0)
        idade_h = (time.time() - (recente.st_mtime or 0)) / 3600.0
        return {
            "nome": recente.filename,
            "url": f"{PUBLIC_BASE_URL}/{recente.filename}",
            "idade_horas": round(idade_h, 2),
            "expirado": idade_h > VALIDADE_HORAS,
            "gerado_em": datetime.fromtimestamp(recente.st_mtime or 0, FUSO).isoformat(),
        }
    finally:
        sftp.close()
        transport.close()


# ------------------------------------------------------------------ modelos


class ItemCotacao(BaseModel):
    codigo: str = ""
    descricao: str
    quantidade: float
    unitario: float
    disponivel: float = 0


class Alternativa(BaseModel):
    descricao: str
    unitario: float


class Pendencia(BaseModel):
    descricao: str
    quantidade: float = 0
    motivo: str = ""
    alternativas: List[Alternativa] = Field(default_factory=list)


class CotacaoRequest(BaseModel):
    numero: Optional[str] = None
    cliente: str
    telefone: str = ""
    atendimento: str = "Ze (digital)"

    # Cadastro completo. Ausente = a caixa do cliente sai na forma reduzida.
    codigo_cliente: Optional[str] = None
    endereco: Optional[str] = None
    numero_endereco: Optional[str] = None
    bairro: Optional[str] = None
    complemento: Optional[str] = None
    cidade: Optional[str] = None
    uf: Optional[str] = None
    cep: Optional[str] = None
    cpf_cnpj: Optional[str] = None
    ie: Optional[str] = None

    itens: List[ItemCotacao]
    pendentes: List[Pendencia] = Field(default_factory=list)
    nao_localizados: List[str] = Field(default_factory=list)

    # Por padrao uma cotacao ainda valida e reaproveitada em vez de gerar arquivo novo.
    regerar: bool = False


class CotacaoResponse(BaseModel):
    numero: str
    pdf_url: str
    reaproveitado: bool = False
    expirado: bool = False
    gerado_em: Optional[str] = None
    idade_horas: Optional[float] = None


def _validar_numero(numero: str) -> str:
    numero = (numero or "").strip().upper()
    if not RE_NUMERO.match(numero):
        raise HTTPException(400, "Numero de cotacao invalido.")
    return numero


def _para_renderizador(req: CotacaoRequest, numero: str) -> dict:
    momento = agora()
    dados = req.model_dump()
    dados["numero_endereco"] = req.numero_endereco
    # O renderizador espera "numero" como o numero da CASA e "protocolo" como o numero
    # da cotacao. Nomes diferentes de proposito: no formulario do Winthor os dois
    # aparecem, e trocar um pelo outro imprimiria o numero da cotacao no endereco.
    dados["numero"] = req.numero_endereco or ""
    dados["protocolo"] = numero
    dados["data"] = f"{momento:%d/%m/%Y}"
    dados["hora"] = f"{momento:%H:%M:%S}"
    dados["total_pedido"] = sum(i.quantidade * i.unitario for i in req.itens)
    dados["total_disponivel"] = sum(i.disponivel * i.unitario for i in req.itens)
    return dados


# ------------------------------------------------------------------ rotas


@router.post("/cotacao", response_model=CotacaoResponse)
def criar_cotacao(req: CotacaoRequest, authorization: str = Header(None)):
    checar_token(authorization)

    if not req.itens:
        raise HTTPException(400, "Cotacao sem itens.")

    numero = _validar_numero(req.numero) if req.numero else gerar_numero()

    if not req.regerar:
        achado = procurar(numero)
        if achado and not achado["expirado"]:
            return CotacaoResponse(
                numero=numero,
                pdf_url=achado["url"],
                reaproveitado=True,
                gerado_em=achado["gerado_em"],
                idade_horas=achado["idade_horas"],
            )

    pdf = gerar_pdf(_para_renderizador(req, numero))
    nome = f"{numero}-{secrets.token_urlsafe(12)}.pdf"
    url = subir(pdf, nome)
    return CotacaoResponse(numero=numero, pdf_url=url, gerado_em=agora().isoformat())


@router.get("/cotacao/{numero}", response_model=CotacaoResponse)
def buscar_cotacao(numero: str, authorization: str = Header(None)):
    """Cliente voltou e disse o numero. Nao regera nada — so localiza.

    Vencida, devolve 200 com expirado=true em vez de erro: quem chama precisa da
    diferenca entre "nao existe" (404) e "existe mas venceu", para responder ao cliente
    que a cotacao caducou e refazer com preco de hoje.
    """
    checar_token(authorization)
    numero = _validar_numero(numero)

    achado = procurar(numero)
    if not achado:
        raise HTTPException(404, "Cotacao nao encontrada.")

    return CotacaoResponse(
        numero=numero,
        pdf_url="" if achado["expirado"] else achado["url"],
        reaproveitado=not achado["expirado"],
        expirado=achado["expirado"],
        gerado_em=achado["gerado_em"],
        idade_horas=achado["idade_horas"],
    )


@router.post("/cotacao/expurgo")
def expurgar_cotacoes(dias: int = 30, authorization: str = Header(None)):
    """Apaga PDFs de cotacao com mais de `dias` dias.

    **Nao esta agendada.** A decisao de 03/09 foi nao expurgar cotacao, porque a validade
    esta impressa no documento. A rota existe para que a decisao continue sendo uma
    decisao e nao uma impossibilidade: sem ninguem chamar, nada e apagado.

    Vale lembrar o que a decisao aceita: a validade impressa governa o que o cliente pode
    exigir, nao quem consegue baixar. Sem expurgo, todo PDF ja gerado fica publicamente
    baixavel para sempre, cada um com nome, endereco, CPF e IE. O token no nome protege
    contra enumeracao, nao contra link vazado.
    """
    checar_token(authorization)
    if dias < 1:
        raise HTTPException(400, "dias tem de ser 1 ou mais.")

    limite = time.time() - dias * 86400
    sftp, transport = _sftp()
    apagados = 0
    try:
        try:
            entradas = sftp.listdir_attr(BASE_DIR)
        except IOError:
            return {"apagados": 0, "dias": dias}
        for entrada in entradas:
            fim = entrada.filename.lower()
            if (fim.endswith(".pdf") or fim.endswith(".part")) and (entrada.st_mtime or 0) < limite:
                sftp.remove(f"{BASE_DIR}/{entrada.filename}")
                apagados += 1
    finally:
        sftp.close()
        transport.close()
    return {"apagados": apagados, "dias": dias}


@router.get("/cotacao/{numero}/existe")
def existe_cotacao(
    numero: str,
    authorization: str = Header(None),
    incluir_url: bool = Query(False, description="devolve a URL mesmo vencida"),
):
    """Checagem barata, sem 404, para o fluxo decidir o caminho antes de responder."""
    checar_token(authorization)
    numero = _validar_numero(numero)
    achado = procurar(numero)
    if not achado:
        return {"numero": numero, "existe": False}
    resposta = {
        "numero": numero,
        "existe": True,
        "expirado": achado["expirado"],
        "gerado_em": achado["gerado_em"],
        "idade_horas": achado["idade_horas"],
    }
    if incluir_url or not achado["expirado"]:
        resposta["pdf_url"] = achado["url"]
    return resposta
