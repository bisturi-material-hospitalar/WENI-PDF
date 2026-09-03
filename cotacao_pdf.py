"""Cotacao em PDF no formulario do orcamento do Winthor.

Modulo de renderizacao da bridge. Nao conhece storage nem HTTP: recebe o dicionario da
cotacao e devolve bytes. Quem grava e publica e o cotacao_api.py.

Escopo (decisao de 03/09/2026): o LAYOUT imita o orcamento do Winthor, os DADOS continuam
sendo os da cotacao. Campo que so existe no ERP nao e inventado:

    Orcamento: 257000097   -> Cotacao: <numero do atendimento>, nao numeracao do ERP
    Cliente: 306367 + CPF  -> imprime o cadastro quando ele existe; senao, nome e telefone
    Codprod                -> codigo do catalogo (refId). Se refId == codprod do Winthor
                              nao foi verificado; enquanto nao for, a coluna pode sair
    Valor FRETE            -> a definir; frete exige CEP e e do agente de checkout
    Forma de Pagto         -> a definir no fechamento
    Separador/Conferente   -> removidos: pertencem a separacao fisica de pedido

O rodape marca a area vaga da folha para que ninguem acrescente item a mao depois de
impresso. O logotipo entra de logo_bisturi.png, na mesma pasta deste arquivo.
"""

import io
import os

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.platypus import (
    Flowable,
    Image,
    KeepTogether,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

# Logotipo oficial, enviado em 03/09/2026, ja recortado da moldura branca do original.
AQUI = os.path.dirname(os.path.abspath(__file__))
LOGO_ARQUIVO = os.path.join(AQUI, "logo_bisturi.png")


def _proporcao(caminho: str) -> float:
    largura, altura = ImageReader(caminho).getSize()
    return altura / float(largura)


PRETO = colors.black
VERMELHO = colors.HexColor("#CC0000")
CINZA = colors.HexColor("#666666")

P = ParagraphStyle("p", fontName="Helvetica", fontSize=8.5, leading=10.5)
PB = ParagraphStyle("pb", parent=P, fontName="Helvetica-Bold")
PI = ParagraphStyle("pi", parent=P, fontName="Helvetica-BoldOblique")
EMPRESA = ParagraphStyle("empresa", parent=P, fontName="Helvetica-Bold", fontSize=12.5,
                         alignment=TA_CENTER, leading=15)
DIR = ParagraphStyle("dir", parent=P, alignment=TA_RIGHT)
DIRB = ParagraphStyle("dirb", parent=PB, alignment=TA_RIGHT)
CAB = ParagraphStyle("cab", parent=P, fontName="Helvetica-BoldOblique", fontSize=8.5)
CABD = ParagraphStyle("cabd", parent=CAB, alignment=TA_RIGHT)
ALERTA = ParagraphStyle("alerta", parent=P, fontName="Helvetica-BoldOblique",
                        fontSize=7.8, textColor=VERMELHO, leading=9.5)
NOTA = ParagraphStyle("nota", parent=P, fontSize=7.2, textColor=CINZA, leading=9)
SUBITEM = ParagraphStyle("subitem", parent=P, fontSize=7.4, textColor=VERMELHO, leading=9)

SEM_PADDING = [
    ("LEFTPADDING", (0, 0), (-1, -1), 3),
    ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ("TOPPADDING", (0, 0), (-1, -1), 1.5),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 1.5),
    ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
]


class AreaProtegida(Flowable):
    """Preenche o espaco vago do fim do documento com marca d'agua.

    Motivo: uma cotacao impressa com um palmo de papel em branco no rodape convida a
    escrever item a mao depois. O flowable ocupa TODA a altura que sobrou no frame, marca
    o inicio da area com uma linha e um aviso, e cobre o resto com a marca repetida em
    diagonal. Qualquer acrescimo manuscrito passa a ser visivelmente sobre a marca.
    """

    ALTURA_MINIMA = 26  # abaixo disso nao sobra area para marcar

    def wrap(self, largura_disponivel, altura_disponivel):
        self.largura = largura_disponivel
        self.altura = max(altura_disponivel, 0)
        return self.largura, self.altura

    def draw(self):
        if self.altura < self.ALTURA_MINIMA:
            return

        c = self.canv
        c.saveState()

        topo = self.altura

        # Linha e aviso que delimitam o fim da cotacao.
        c.setStrokeColor(colors.HexColor("#999999"))
        c.setLineWidth(0.5)
        c.setDash(2, 2)
        c.line(0, topo - 10, self.largura, topo - 10)
        c.setDash()
        c.setFillColor(colors.HexColor("#999999"))
        c.setFont("Helvetica-Oblique", 6.8)
        c.drawCentredString(
            self.largura / 2.0, topo - 19,
            "nada abaixo desta linha faz parte da cotação")

        # Marca repetida em diagonal, no espaco que sobrou.
        area = topo - 26
        if area <= 20:
            c.restoreState()
            return

        # Recorta na area vaga: sem isso a diagonal invade as margens da pagina.
        recorte = c.beginPath()
        recorte.rect(0, 0, self.largura, area)
        c.clipPath(recorte, stroke=0, fill=0)

        # A marca do rodape e TEXTO, nao o logotipo: o desenho azulejado ficou pesado e o
        # cabecalho ja carrega o logo. Decisao de 03/09/2026.
        # A cor tem de carregar o alpha: setFillColor depois de setFillAlpha zera a
        # transparencia, porque o objeto Color traz alpha=1 por padrao.
        fonte, corpo = "Helvetica-Bold", 9
        c.setFillColor(colors.Color(0.8, 0, 0, alpha=0.13))
        c.setFont(fonte, corpo)

        selo = "BISTURI MATERIAL HOSPITALAR   ·   COTAÇÃO   ·   "
        largura_texto = c.stringWidth(selo, fonte, corpo)
        linha = selo * (int(self.largura * 1.8 / largura_texto) + 2)
        passo = 24

        c.rotate(-14)
        # Depois de girar, o retangulo recortado exige comecar acima e a esquerda.
        y = area + self.largura * 0.26
        while y > -passo:
            c.drawString(-largura_texto * (0.5 if int(y / passo) % 2 else 0.0) - 30, y, linha)
            y -= passo

        c.restoreState()


def num(valor: float, casas: int = 2) -> str:
    texto = f"{valor:,.{casas}f}"
    return texto.replace(",", "@").replace(".", ",").replace("@", ".")


def _rotulo(rotulo: str, valor: str) -> list:
    return [Paragraph(rotulo, DIRB), Paragraph(valor, P)]


def _par(*pares: tuple) -> str:
    """Monta 'VALOR   <b>Rotulo:</b> valor' na mesma linha, como no formulario do ERP."""
    partes = []
    for rotulo, valor in pares:
        if not valor:
            continue
        partes.append(f"<b>{rotulo}</b> {valor}" if rotulo else str(valor))
    return "&nbsp;&nbsp;&nbsp;&nbsp;".join(partes)


def _bloco_cliente(dados: dict, L: float) -> Table:
    """Caixa do cliente.

    Duas formas, escolhidas pelo que existe nos dados — nunca por preenchimento a mao:

    - cadastro completo (o cliente foi identificado): reproduz as linhas do orcamento do
      Winthor, com endereco, cidade/UF/CEP, telefone, CPF/CNPJ e IE;
    - so contato (caso normal da cotacao por WhatsApp): nome, telefone e uma linha
      dizendo que o resto do cadastro e coletado no fechamento.

    O criterio e a presenca de endereco. Campo ausente simplesmente nao imprime rotulo:
    melhor uma linha mais curta do que um rotulo vazio convidando a escrever por cima.
    """
    linhas = [_rotulo("Cliente:", _par(("", dados.get("codigo_cliente")), ("", dados["cliente"])))]

    if dados.get("endereco"):
        linhas += [
            _rotulo("Endereço:", _par(("", dados["endereco"]), ("No:", dados.get("numero")))),
            _rotulo("Bairro:", _par(("", dados.get("bairro")),
                                    ("Complemento:", dados.get("complemento")))),
            _rotulo("Cidade:", _par(("", dados.get("cidade")), ("UF:", dados.get("uf")),
                                    ("CEP:", dados.get("cep")))),
            _rotulo("Telefones:", _par(("", dados.get("telefone")),
                                       ("CPF/CNPJ:", dados.get("cpf_cnpj")),
                                       ("IE:", dados.get("ie")))),
        ]
    else:
        linhas += [
            _rotulo("Telefone:", dados.get("telefone", "")),
            [Paragraph("Cadastro:", DIRB),
             Paragraph("endereço, CPF/CNPJ e CEP são coletados no fechamento do pedido", NOTA)],
        ]

    caixa = Table(linhas, colWidths=[L * 0.14, L * 0.48])
    caixa.setStyle(TableStyle(SEM_PADDING + [("BOX", (0, 0), (-1, -1), 0.9, PRETO)]))
    return caixa


def gerar_pdf(dados: dict, caminho=None) -> bytes:
    """Renderiza a cotacao. Sem `caminho`, devolve os bytes do PDF em memoria."""
    destino = caminho or io.BytesIO()
    doc = SimpleDocTemplate(
        destino, pagesize=A4,
        leftMargin=12 * mm, rightMargin=12 * mm, topMargin=10 * mm, bottomMargin=12 * mm,
        title="Cotação Bisturi", author="Bisturi Distribuidora de Material Hospitalar",
    )
    L = doc.width
    fluxo = []

    # ------------------------------------------------- caixa do topo (empresa)
    topo = Table(
        [
            [Paragraph("BISTURI DISTR. DE MAT. HOSP. LTDA", EMPRESA), "", ""],
            # CNPJ e IE informados pelo usuario em 03/09/2026.
            [Paragraph("TEL: 3601-4001", PB),
             Paragraph("CNPJ: 32.561.144/0004-56", PB),
             Paragraph("IE: 75.765.894", PB)],
        ],
        colWidths=[L * 0.34, L * 0.36, L * 0.30],
    )
    topo.setStyle(TableStyle(SEM_PADDING + [
        ("SPAN", (0, 0), (-1, 0)),
        ("BOX", (0, 0), (-1, -1), 0.9, PRETO),
        ("LINEBELOW", (0, 0), (-1, 0), 0.9, PRETO),
        ("TOPPADDING", (0, 0), (-1, 0), 3),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 3),
    ]))
    fluxo += [topo, Spacer(1, 3)]

    # ------------------------------- caixa do cliente + bloco da cotacao/logo
    cliente = _bloco_cliente(dados, L)

    identificacao = Table(
        [
            [Paragraph("Cotação:", DIRB), Paragraph(dados["protocolo"], DIRB)],
            [Paragraph("Emissão:", DIRB), Paragraph(dados["data"], DIRB)],
            [Paragraph("Hora:", DIRB), Paragraph(dados["hora"], DIRB)],
        ],
        colWidths=[L * 0.19, L * 0.19],
    )
    identificacao.setStyle(TableStyle(SEM_PADDING))

    largura_logo = L * 0.33
    marca = Image(LOGO_ARQUIVO, width=largura_logo,
                  height=largura_logo * _proporcao(LOGO_ARQUIVO))
    marca.hAlign = "CENTER"
    logo = Table([[marca]], colWidths=[L * 0.38])
    logo.setStyle(TableStyle(SEM_PADDING))

    cabecalho = Table(
        [[cliente, [identificacao, Spacer(1, 2), logo]]],
        colWidths=[L * 0.62, L * 0.38],
    )
    cabecalho.setStyle(TableStyle(SEM_PADDING + [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (0, 0), 0),
        ("RIGHTPADDING", (1, 0), (1, 0), 0),
    ]))
    fluxo += [cabecalho, Spacer(1, 3)]

    # ------------------------------------------------------------- advertencias
    fluxo += [
        Paragraph("Cotação válida por 24 horas. Produtos sujeitos a disponibilidade de estoque", PB),
        Paragraph("Não efetuamos troca de produtos descartáveis, de uso íntimo ou com embalagem violada.", ALERTA),
        Paragraph(f"<b>Atendimento:</b> {dados['atendimento']}", P),
        Spacer(1, 2),
    ]

    # ------------------------------------------------------------------- itens
    linhas = [[
        Paragraph("Código", CAB), Paragraph("Descrição", CAB),
        Paragraph("Quant", CABD), Paragraph("Vl Unit", CABD), Paragraph("Vl Total", CABD),
    ]]
    marcas_parciais = []
    for item in dados["itens"]:
        descricao = [Paragraph(item["descricao"], P)]
        if item["disponivel"] < item["quantidade"]:
            descricao.append(Paragraph(
                f"disponível hoje: {num(item['disponivel'], 0)} de {num(item['quantidade'], 0)}"
                f" &mdash; {num(item['disponivel'] * item['unitario'])}", SUBITEM))
            marcas_parciais.append(len(linhas))
        linhas.append([
            Paragraph(item["codigo"], P),
            descricao,
            Paragraph(num(item["quantidade"], 2), DIR),
            Paragraph(num(item["unitario"], 2), DIR),
            Paragraph(num(item["quantidade"] * item["unitario"]), DIR),
        ])

    tabela = Table(
        linhas, repeatRows=1,
        colWidths=[L * 0.09, L * 0.55, L * 0.10, L * 0.12, L * 0.14],
    )
    estilo = SEM_PADDING + [
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LINEBELOW", (0, 0), (-1, 0), 0.9, PRETO),
        ("TOPPADDING", (0, 1), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 1), (-1, -1), 3),
    ]
    for i in range(1, len(linhas) - 1):
        estilo.append(("LINEBELOW", (0, i), (-1, i), 0.25, colors.HexColor("#BBBBBB")))
    tabela.setStyle(TableStyle(estilo))
    fluxo += [tabela]

    # ------------------------------------------------------------------ totais
    # A linha DISPONIVEL HOJE so aparece quando ela diz algo diferente do SUBTOTAL, ou
    # seja, quando algum item tem menos estoque do que o pedido. Com a lista inteira
    # disponivel os dois valores seriam iguais e a repeticao so ocupa espaco.
    tem_saldo_parcial = any(i["disponivel"] < i["quantidade"] for i in dados["itens"])

    linhas_totais = [
        [Paragraph(f"TOTAL ITENS : {len(dados['itens'])}", CAB),
         Paragraph("Frete: a definir", CAB),
         Paragraph("SUBTOTAL :", CABD),
         Paragraph(num(dados["total_pedido"]), DIRB)],
        [Paragraph("Forma de Pagto : a definir", CAB),
         "",
         Paragraph("DISPONÍVEL HOJE :", CABD) if tem_saldo_parcial else "",
         Paragraph(num(dados["total_disponivel"]), DIRB) if tem_saldo_parcial else ""],
        [Paragraph("Obs Entrega :", CAB), "", "", ""],
    ]

    totais = Table(
        linhas_totais,
        colWidths=[L * 0.34, L * 0.24, L * 0.28, L * 0.14],
    )
    totais.setStyle(TableStyle(SEM_PADDING + [
        ("LINEABOVE", (0, 0), (-1, 0), 0.9, PRETO),
        ("LINEBELOW", (0, -1), (-1, -1), 0.9, PRETO),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]))
    fluxo += [totais, Spacer(1, 6)]

    # -------------------------------------------------------------- pendencias
    if dados.get("pendentes"):
        bloco = [Paragraph("ITENS QUE PRECISAM DA SUA CONFIRMAÇÃO", CAB), Spacer(1, 2)]
        for p in dados["pendentes"]:
            bloco.append(Paragraph(
                f"<b>{p['descricao']}</b> ({num(p['quantidade'], 0)}) &mdash; {p['motivo']}", P))
            for letra, alt in zip("abcde", p.get("alternativas", [])):
                bloco.append(Paragraph(
                    f"&nbsp;&nbsp;&nbsp;<b>{letra})</b> {alt['descricao']} &mdash; {num(alt['unitario'])}", P))
            bloco.append(Spacer(1, 2))
        if dados.get("nao_localizados"):
            bloco.append(Paragraph(
                "<b>Não localizados:</b> " + "; ".join(dados["nao_localizados"])
                + ". Envie a marca, o código ou uma foto da embalagem.", P))
        caixa = Table([[bloco]], colWidths=[L])
        caixa.setStyle(TableStyle(SEM_PADDING + [
            ("BOX", (0, 0), (-1, -1), 0.9, PRETO),
            ("TOPPADDING", (0, 0), (-1, -1), 4),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ]))
        fluxo += [KeepTogether(caixa), Spacer(1, 6)]

    # ------------------------------------------------------------------ rodape
    # As duas linhas de advertencia de entrega existem no orcamento do Winthor e foram
    # pedidas de volta em 03/09. No original a segunda esta escrita "RECLAMACOES" sem
    # cedilha; aqui sai com a grafia correta.
    aviso = Table(
        [[Paragraph("Favor conferir mercadoria no ato da entrega", CAB),
          Paragraph("NÃO ACEITAMOS RECLAMAÇÕES POSTERIORES",
                    ParagraphStyle("avisoc", parent=CAB, alignment=TA_CENTER))]],
        colWidths=[L * 0.45, L * 0.55],
    )
    aviso.setStyle(TableStyle(SEM_PADDING + [
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LEFTPADDING", (0, 0), (0, 0), 0),
    ]))

    fluxo += [
        aviso,
        Paragraph(
            "Este documento é uma cotação: preços e disponibilidade do momento da consulta, "
            "sujeitos a confirmação pelo time de vendas. Não reserva estoque, não inclui frete "
            "e não substitui o pedido.", NOTA),
        AreaProtegida(),
    ]

    doc.build(fluxo)

    if caminho:
        with open(caminho, "rb") as arquivo:
            return arquivo.read()
    return destino.getvalue()


EXEMPLO = {
    "cliente": "CLIENTE DE TESTE LTDA",
    "telefone": "21 90000-0000",
    "protocolo": "WA-8831",
    "data": "03/09/2026",
    "hora": "10:42:07",
    "atendimento": "Zé (digital)",
    "itens": [
        {"codigo": "19561", "descricao": "AGULHA PEN INSULINA 04MM WILTEX",
         "quantidade": 100, "unitario": 0.40, "disponivel": 100},
        {"codigo": "14847", "descricao": "LUVA PROCEDIMENTOS POWDER FREE SEM PÓ TAM M - 100 UN",
         "quantidade": 13, "unitario": 49.90, "disponivel": 13},
        {"codigo": "14846", "descricao": "LUVA PROCEDIMENTOS POWDER FREE SEM PÓ TAM P - 100 UN",
         "quantidade": 10, "unitario": 49.90, "disponivel": 1},
        {"codigo": "20118", "descricao": "ATADURA DE CREPOM 15CM X 1,80M",
         "quantidade": 40, "unitario": 1.50, "disponivel": 40},
        {"codigo": "17203", "descricao": "ESPARADRAPO MICROPORE 10CM X 4,5M",
         "quantidade": 12, "unitario": 22.00, "disponivel": 12},
    ],
    "pendentes": [
        {"descricao": "luva cirúrgica 9.0", "quantidade": 12,
         "motivo": "você pediu 9.0 e o que temos é 7.0 ou 8.5",
         "alternativas": [
             {"descricao": "LUVA CIRÚRGICA DE LÁTEX MEDIX - SEM PÓ - 8.5 - PAR", "unitario": 2.90},
             {"descricao": "LUVA CIRÚRGICA LIFEULTRA POWDERFREE ESTÉRIL SEM PÓ - 7.0", "unitario": 2.90},
         ]},
        {"descricao": "pinça clínica", "quantidade": 24,
         "motivo": "temos 3 produtos com esse nome e preços diferentes",
         "alternativas": [
             {"descricao": "PINÇA CLÍNICA PARA ALGODÃO ABC NR 17", "unitario": 22.00},
             {"descricao": "PINÇA CLÍNICA PARA ALGODÃO ABC NR 20", "unitario": 26.50},
         ]},
    ],
    "nao_localizados": ["tomógrafo portátil xyz"],
}

# Caso limpo: tudo localizado, tudo com estoque cheio, nada a confirmar. Serve para ver o
# formulario sem a caixa de pendencias e sem a linha vermelha de saldo parcial. Quando
# nao ha pendencia, DISPONIVEL HOJE == SUBTOTAL — e assim mesmo, os dois totais ficam,
# porque a ausencia de diferenca tambem e informacao para quem le.
EXEMPLO_SIMPLES = {
    "cliente": "CLINICA SAO JORGE LTDA",
    "telefone": "21 98812-4470",
    "protocolo": "WA-9104",
    "data": "03/09/2026",
    "hora": "14:07:52",
    "atendimento": "Zé (digital)",
    "itens": [
        {"codigo": "19561", "descricao": "AGULHA PEN INSULINA 04MM WILTEX",
         "quantidade": 100, "unitario": 0.40, "disponivel": 100},
        {"codigo": "20118", "descricao": "ATADURA DE CREPOM 15CM X 1,80M",
         "quantidade": 40, "unitario": 1.50, "disponivel": 40},
        {"codigo": "17203", "descricao": "ESPARADRAPO MICROPORE 10CM X 4,5M",
         "quantidade": 12, "unitario": 22.00, "disponivel": 12},
        {"codigo": "14847", "descricao": "LUVA PROCEDIMENTOS POWDER FREE SEM PÓ TAM M - 100 UN",
         "quantidade": 13, "unitario": 49.90, "disponivel": 13},
        {"codigo": "11982", "descricao": "SERINGA DESCARTÁVEL 10ML SEM AGULHA - 100 UN",
         "quantidade": 6, "unitario": 34.50, "disponivel": 6},
        {"codigo": "15644", "descricao": "COMPRESSA DE GAZE 7,5X7,5CM 11 FIOS - PCT 500 UN",
         "quantidade": 8, "unitario": 27.90, "disponivel": 8},
    ],
    "pendentes": [],
    "nao_localizados": [],
}


# Cadastro completo: cliente identificado, mesmos campos do orcamento do Winthor. Dados
# de teste fornecidos pelo usuario em 03/09/2026.
EXEMPLO_COMPLETO = {
    "codigo_cliente": "999999",
    "cliente": "CLIENTE DE TESTE LTDA",
    "endereco": "RUA EXEMPLO",
    "numero": "56",
    "bairro": "CENTRO",
    "complemento": "Casa",
    "cidade": "RIO DE JANEIRO",
    "uf": "RJ",
    "cep": "20000000",
    "telefone": "21900000000",
    "cpf_cnpj": "000.000.000-00",
    "ie": "ISENTO",
    "protocolo": "WA-9217",
    "data": "03/09/2026",
    "hora": "15:26:41",
    "atendimento": "Zé (digital)",
    "itens": [
        {"codigo": "19561", "descricao": "AGULHA PEN INSULINA 04MM WILTEX",
         "quantidade": 100, "unitario": 0.40, "disponivel": 100},
        {"codigo": "15644", "descricao": "COMPRESSA DE GAZE 7,5X7,5CM 11 FIOS - PCT 500 UN",
         "quantidade": 8, "unitario": 27.90, "disponivel": 8},
        {"codigo": "11982", "descricao": "SERINGA DESCARTÁVEL 10ML SEM AGULHA - 100 UN",
         "quantidade": 6, "unitario": 34.50, "disponivel": 6},
        {"codigo": "20118", "descricao": "ATADURA DE CREPOM 15CM X 1,80M",
         "quantidade": 40, "unitario": 1.50, "disponivel": 40},
    ],
    "pendentes": [],
    "nao_localizados": [],
}


def _fechar_totais(dados: dict) -> dict:
    dados["total_pedido"] = sum(i["quantidade"] * i["unitario"] for i in dados["itens"])
    dados["total_disponivel"] = sum(i["disponivel"] * i["unitario"] for i in dados["itens"])
    return dados


_fechar_totais(EXEMPLO)
_fechar_totais(EXEMPLO_SIMPLES)
_fechar_totais(EXEMPLO_COMPLETO)


if __name__ == "__main__":
    # Smoke test de layout: gera os dois casos e informa o tamanho.
    for nome, dados in (("cotacao_cadastro", EXEMPLO_COMPLETO),
                        ("cotacao_pendencias", EXEMPLO),
                        ("cotacao_simples", EXEMPLO_SIMPLES)):
        conteudo = gerar_pdf(dados)
        with open(f"{nome}.pdf", "wb") as f:
            f.write(conteudo)
        print(f"{nome}.pdf  {len(conteudo)} bytes")
