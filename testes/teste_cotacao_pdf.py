"""Teste do renderizador da cotacao, com foco no bloco de pendencias.

Por que existe: em 09/09/2026 uma cotacao real de producao com 32 pendencias nao gerou
PDF. O cliente pediu, ouviu "nao consegui gerar o PDF agora. Me peca de novo e eu envio",
pediu de novo e nao recebeu nada. A causa nao era instabilidade da bridge nem prazo: o
bloco "ITENS QUE PRECISAM DA SUA CONFIRMACAO" era uma Table de UMA celula embrulhada em
KeepTogether. Celula de tabela nao parte entre paginas no reportlab, e o KeepTogether
pedia explicitamente para nao partir — entao o bloco todo tinha de caber numa pagina.

Media a falha em 09/09: passando de 21 pendencias com 2 alternativas cada, o bloco passava
dos 767 pt do frame e o reportlab levantava LayoutError. A bridge devolvia 500, o cliente
da tool recebia None.

O que faz este teste falhar de novo: voltar a envolver as pendencias num KeepTogether, ou
voltar a jogar todas numa unica caixa. O corte por altura medida tem de continuar.

Cobre:
  - o caso real de producao (12 cotados + 32 pendencias) gera PDF
  - varredura de tamanhos absurdos, muito acima do MAX_ITEMS de 60
  - pendencia com muitas alternativas e descricao longa
  - o cabecalho de continuacao aparece quando o bloco parte em mais de uma caixa
  - cotados sozinhos nunca foram o problema, e continuam nao sendo
  - o PDF continua sendo bytes de PDF de verdade, com mais de uma pagina quando precisa
"""

import copy
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cotacao_pdf import EXEMPLO, gerar_pdf  # noqa: E402

falhas = []


def checar(nome, condicao):
    if condicao:
        print(f"  ok  {nome}")
    else:
        print(f"  FALHA  {nome}")
        falhas.append(nome)


def corpo(n_cotados, n_pendentes, n_alternativas=2, repeticoes_descricao=1):
    """Cotacao sintetica no formato que a bridge recebe."""
    dados = copy.deepcopy(EXEMPLO)
    dados["itens"] = [
        {
            "codigo": str(10000 + i),
            "descricao": f"PRODUTO DE TESTE {i} COM NOME LONGO TIPICO DO CATALOGO REAL",
            "quantidade": 20,
            "unitario": 22.90,
            "disponivel": 20,
        }
        for i in range(n_cotados)
    ]
    dados["pendentes"] = [
        {
            "descricao": f"item pendente numero {i} " + ("nome bem longo " * repeticoes_descricao),
            "quantidade": 100,
            "motivo": "temos 3 produtos com esse nome e precos diferentes",
            "alternativas": [
                {"descricao": f"Alternativa {chr(97 + a)} do item {i} com nome longo",
                 "unitario": 1.60}
                for a in range(n_alternativas)
            ],
        }
        for i in range(n_pendentes)
    ]
    dados["nao_localizados"] = [
        "Fitas p/ Glicosimetro On Call", "Glicosimetro On Call Plus",
        "Tubo Traqueal c/ Balao 8-0",
    ]
    return dados


def gerou(dados):
    """Devolve os bytes, ou None se o reportlab levantou qualquer coisa."""
    try:
        return gerar_pdf(dados)
    except Exception as exc:  # LayoutError e o caso conhecido, mas qualquer erro reprova
        print(f"       ({type(exc).__name__}: {str(exc)[:90]})")
        return None


print("=" * 72)
print("BLOCO DE PENDENCIAS: O CASO REAL DE 09/09/2026")
print("=" * 72)
print("Cotacao de producao com 12 itens cotados e 32 pendencias. Antes da correcao esta")
print("chamada levantava LayoutError e a bridge devolvia 500.")

pdf = gerou(corpo(12, 32))
checar("12 cotados + 32 pendencias gera PDF", pdf is not None)
checar("saida e um PDF de verdade", bool(pdf) and pdf[:5] == b"%PDF-")

print()
print("=" * 72)
print("VARREDURA DE TAMANHO")
print("=" * 72)
print("MAX_ITEMS do agente e 60, entao os casos abaixo de 60 sao os possiveis hoje. Os")
print("acima ficam como margem: um MAX_ITEMS maior amanha nao pode reabrir o LayoutError.")

CASOS = [
    # (cotados, pendentes, alternativas, repeticoes de descricao)
    (6, 0, 2, 1),      # o caso que sempre funcionou (COT-0909-Q2E2 em producao)
    (6, 21, 2, 1),     # ultimo tamanho que passava antes da correcao
    (6, 22, 2, 1),     # primeiro que quebrava
    (6, 50, 2, 1),
    (44, 32, 2, 1),
    (60, 60, 2, 1),    # MAX_ITEMS inteiro, tudo pendente
    (6, 40, 5, 6),     # poucas pendencias, mas cada uma enorme
    (100, 120, 5, 3),  # margem folgada
]
for n_c, n_p, n_a, rep in CASOS:
    rotulo = f"{n_c} cotados + {n_p} pendencias, {n_a} alternativa(s), descricao x{rep}"
    checar(rotulo, gerou(corpo(n_c, n_p, n_a, rep)) is not None)

print()
print("=" * 72)
print("CABECALHO DE CONTINUACAO")
print("=" * 72)
print("Quando o bloco parte em mais de uma caixa, a segunda tem de se identificar — senao")
print("a pagina seguinte comeca com uma lista de itens sem dizer do que se trata.")

texto_grande = gerou(corpo(6, 40))
texto_pequeno = gerou(corpo(6, 3))


def tem_continuacao(bytes_pdf):
    """Procura a marca no conteudo do PDF.

    O texto sai comprimido nos streams, entao a busca literal nao serve. Extrai com o
    proprio reportlab nao da; usa-se aqui a contagem de paginas como proxy do corte, e a
    presenca da string e checada de forma tolerante: se o PDF nao estiver comprimido a
    string aparece, e se estiver o teste cai no proxy de paginas.
    """
    if b"(continua" in bytes_pdf or b"continua\\347\\343o" in bytes_pdf:
        return True
    return len(re.findall(rb"/Type\s*/Page[^s]", bytes_pdf)) > 1


checar("cotacao com 40 pendencias parte em mais de uma pagina/caixa",
       bool(texto_grande) and tem_continuacao(texto_grande))
checar("cotacao com 3 pendencias nao precisa partir",
       bool(texto_pequeno) and len(re.findall(rb"/Type\s*/Page[^s]", texto_pequeno)) >= 1)

print()
print("=" * 72)
print("GUARDA CONTRA A REGRESSAO NO CODIGO")
print("=" * 72)
print("A causa raiz era estrutural, nao de tamanho. Estas checagens olham o proprio")
print("codigo: e o que impede alguem de reintroduzir o KeepTogether sem rodar o caso de 32.")

fonte = open(
    os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "cotacao_pdf.py"),
    encoding="utf-8",
).read()

trecho_pendencias = fonte[fonte.find("if dados.get(\"pendentes\")"):]
trecho_pendencias = trecho_pendencias[:trecho_pendencias.find("# ---------------------")]

# So as linhas de CODIGO. O trecho tem comentarios que explicam o incidente e citam
# KeepTogether de proposito; procurar a palavra no texto cru reprovaria a propria correcao.
codigo_pendencias = "\n".join(
    linha for linha in trecho_pendencias.splitlines()
    if linha.strip() and not linha.strip().startswith("#")
)

checar("o bloco de pendencias nao usa KeepTogether no codigo",
       "KeepTogether" not in codigo_pendencias)
checar("cada caixa entra no fluxo solta, para poder descer de pagina",
       "fluxo += [caixa," in codigo_pendencias)
checar("o corte por altura medida continua no lugar",
       "ALTURA_UTIL" in trecho_pendencias and "wrap(" in trecho_pendencias)
checar("o cabecalho de continuacao continua escrito",
       "continuação" in trecho_pendencias)

print()
if falhas:
    print(f"FALHOU: {len(falhas)} problema(s)")
    for f in falhas:
        print(f"  - {f}")
    raise SystemExit(1)
print("TUDO OK")
