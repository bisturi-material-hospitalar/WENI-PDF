# Cotação em PDF — passo a passo de configuração e teste

Rotas novas no mesmo serviço `bridge-danfe`. Nada do DANFE muda: o código da cotação vive
em `cotacao_api.py` e `cotacao_pdf.py`, grava em outra pasta e não importa nada do
`bridge_danfe.py`, que ganhou só duas linhas no fim (import do router e `include_router`).

**A ordem importa**, e não é a intuitiva:

```
1. teste de pasta  ->  2. .htaccess  ->  3. env vars  ->  4. PUSH  ->  5. validar
                                              ->  6. cron do expurgo  ->  7. agente
```

O **push vem antes do cron**: um cron apontando para rota que ainda não existe recebe 404,
e o cron-job.org desabilita o job depois de N falhas (é a opção "the cronjob will be
disabled because of too many failures", no próprio formulário). E o **agente vem por
último**: é a única peça que fala com cliente real, então só sobe depois de a rota estar
provada pelo passo 5.

---

## Passo 1 — Descobrir qual pasta o servidor serve

A raiz da Umbler tem `public`, `public_html` e `undefined`. A pasta `cotacao` foi criada
em duas delas. Só uma é servida em `https://arquivos.bisturi.com.br/cotacao/`, e gravar
na errada produz um link que dá 404 **sem nenhum erro no log da bridge** — o upload
funciona, o arquivo existe, só não é alcançável.

O que já se sabe, verificado por HTTP em 03/09:

```
GET https://arquivos.bisturi.com.br/danfe/     -> 403   (caminho resolve, -Indexes ativo)
GET https://arquivos.bisturi.com.br/cotacao/   -> 403   (caminho resolve; qual pasta, nao diz)
```

E as notas que o serviço entrega em produção estão em `/public/danfe`. Isso torna
`public/` a raiz web quase certa — mas o teste abaixo transforma "quase certa" em certa.

### 1.1 Conectar

FileZilla, **SFTP - SSH File Transfer Protocol**:

| Campo | Valor |
|---|---|
| Host | `arquivos-bisturi-com-br.umbler.net` |
| Porta | `22` |
| Usuário | `umbler` |
| Senha | a mesma de `SFTP_PASSWORD` no Render |

Ligue **Servidor → Forçar exibição de arquivos ocultos**, senão o `.htaccess` fica
invisível e você vai achar que não subiu.

### 1.2 Subir um arquivo-sonda em cada pasta

Os dois arquivos estão neste pacote, com nomes diferentes de propósito — assim cada URL
testa uma pasta, sem ambiguidade:

| Arquivo local | Subir para |
|---|---|
| `ping-public.txt` | `/public/cotacao/` |
| `ping-publichtml.txt` | `/public_html/cotacao/` |

### 1.3 Abrir as duas URLs no navegador

```
https://arquivos.bisturi.com.br/cotacao/ping-public.txt
https://arquivos.bisturi.com.br/cotacao/ping-publichtml.txt
```

A que abrir e mostrar o texto identifica a pasta servida. A outra dará 404.

> Se as **duas** abrirem, existe alias das duas pastas para o mesmo caminho web e
> qualquer uma serve — mas então escolha uma e apague a outra, para não ficar com duas
> fontes do mesmo arquivo.
>
> Se **nenhuma** abrir, `cotacao` não está sob a raiz web: confira em qual pasta o
> `danfe` está no FileZilla e crie a `cotacao` como irmã dele.

### 1.4 Limpar

Apague os dois `ping-*.txt` e a pasta `cotacao` que **não** foi servida. Daqui para
frente, "a pasta" é a que venceu o teste.

---

## Passo 2 — `.htaccess` na pasta servida

**Sem este arquivo o token aleatório no nome do PDF não protege nada**: qualquer pessoa
abre a pasta pelo navegador e lê a lista inteira de cotações. É a mesma exigência que o
`README-deploy.md` faz para a pasta do DANFE.

1. No FileZilla, suba `htaccess-cotacao.txt` para a pasta servida.
2. Renomeie no servidor para `.htaccess` (renomear no servidor é mais simples do que
   criar um arquivo com ponto na frente no Windows).
3. Confirme abrindo `https://arquivos.bisturi.com.br/cotacao/` — tem de dar **403**.

O arquivo também nega `.part`, que é o nome temporário do upload: se uma transferência
morrer no meio, o pedaço não fica servível.

---

## Passo 3 — Variáveis no Render

O Blueprint (`render.yaml`) **só é aplicado na criação do serviço**. Como o
`bridge-danfe` já existe, estas três entram na mão:

Dashboard → `bridge-danfe` → **Environment** → *Add Environment Variable*

| Chave | Valor |
|---|---|
| `COTACAO_BASE_DIR` | a pasta que venceu o passo 1, ex. `/public/cotacao` |
| `COTACAO_PUBLIC_BASE_URL` | `https://arquivos.bisturi.com.br/cotacao` |
| `COTACAO_VALIDADE_HORAS` | `24` |

### Dois nomes errados no dashboard (03/09/2026)

Na lista de variáveis do `bridge-danfe` estão gravadas:

| Gravado | Correto | Efeito |
|---|---|---|
| `SFTP_BASE_SIR` | `SFTP_BASE_DIR` | a variável real fica **vazia** e o código cai nos defaults do arquivo — que discordam entre si |
| `VTEX_ENVIRONMENTE` | `VTEX_ENVIRONMENT` | inofensivo hoje: o default do código é `vtexcommercestable`, o mesmo valor pretendido |

O primeiro é sério. Com `SFTP_BASE_DIR` vazio:

```
_sftp_remote_path   (grava)   -> /public_html/danfe
listar_chaves       (procura) -> /public/danfe
expurgar_antigos    (apaga)   -> /public_html/danfe
```

Grava num caminho e procura em outro. Como as notas aparecem e as URLs funcionam, a
explicação mais provável é que **`public_html` seja link simbólico para `public`** (ou o
contrário) — comum na Umbler. O teste do passo 1 responde: se as **duas** URLs de ping
abrirem, são a mesma pasta e está tudo consistente por acidente.

Se não forem a mesma pasta, algo já está quebrado hoje: a busca de nota por número e por
e-mail nunca encontraria nada, porque procura onde o upload não grava. Vale checar se
esses dois caminhos funcionam em produção — é a resposta mais barata.

Nos dois casos, **corrija o nome**: depender de default que discorda de si mesmo dentro do
arquivo é a mesma falha silenciosa que este runbook existe para evitar.

**Confira no mesmo lugar o `SFTP_BASE_DIR`.** Ele tem de ser a pasta real das notas
(`/public/danfe`). O `render.yaml` do repositório trazia `/public_html/danfe` — corrigido
em 03/09, mas se o dashboard também estiver errado o DANFE está gravando em lugar
diferente do que serve.

Salvar dispara redeploy sozinho.

---

## Passo 4 — Subir o código

Antes de comitar, rode o teste local. Ele não toca em Umbler nem em Render: o SFTP é
substituído por um dicionário em memória.

```powershell
cd C:\Users\e-commerce-06.BISTURI\Downloads\weni-pdf
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt          # traz o reportlab, que e dependencia nova
py testes\teste_cotacao_api.py           # esperado: TUDO OK
py cotacao_pdf.py                        # gera 3 PDFs de amostra para olhar o layout
```

Depois:

```powershell
git add cotacao_api.py cotacao_pdf.py logo_bisturi.png bridge_danfe.py `
        requirements.txt render.yaml testes\teste_cotacao_api.py `
        exemplo-cotacao.json README-cotacao.md htaccess-cotacao.txt
git commit -m "cotacao em PDF: rotas, renderizador e teste"
git push
```

O Render redeploya no push. Acompanhe o log do build: se o `reportlab` falhar na
instalação, o serviço volta com a versão anterior e o DANFE continua no ar.

---

## Passo 5 — Teste ponta a ponta

```powershell
$token = "<BRIDGE_TOKEN>"
$base  = "https://bridge-danfe.onrender.com"
$h     = @{ Authorization = "Bearer $token" }

# 1. servico no ar (a primeira chamada depois de 15 min parado leva 30-50 s: cold start)
Invoke-RestMethod "$base/health"

# 2. gerar a cotacao de exemplo
$r = Invoke-RestMethod -Method Post -Uri "$base/cotacao" -Headers $h `
     -ContentType "application/json" -InFile "exemplo-cotacao.json"
$r

# 3. abrir o PDF — este e o teste que importa: prova pasta, .htaccess e URL de uma vez
Start-Process $r.pdf_url

# 4. cliente voltou e disse o numero: acha sem regerar
Invoke-RestMethod "$base/cotacao/$($r.numero)" -Headers $h

# 5. numero que nao existe: 404 esperado
Invoke-RestMethod "$base/cotacao/WA-0000" -Headers $h
```

O que cada resposta tem de dizer:

| Passo | Esperado |
|---|---|
| 2 | `numero: WA-9217`, `pdf_url` terminando em `WA-9217-<16 caracteres>.pdf`, `reaproveitado: False` |
| 3 | o PDF abre no navegador, com o cadastro completo na caixa do cliente |
| 4 | **a mesma** `pdf_url` do passo 2, com `reaproveitado: True` — não gerou arquivo novo |
| 5 | erro 404 |

Repetir o passo 2 dentro das 24 h tem de devolver `reaproveitado: True` e a mesma URL.
Para forçar um arquivo novo, acrescente `"regerar": true` ao JSON.

### Teste da validade, sem esperar um dia

Mude `COTACAO_VALIDADE_HORAS` para `0` no Render, espere o redeploy e chame o passo 4: a
resposta tem de vir com `expirado: true` e `pdf_url` vazia. Depois devolva para `24`.

---

## Cron — o que muda e o que nao muda

O cron de vocês bate em `POST /pregerar` a cada 10 min em horário comercial, o que
mantém o container acordado fazendo trabalho útil. **A cotação não precisa de cron
próprio para isso:** é o mesmo container, então quando o serviço está de pé para o DANFE
está de pé para a cotação.

Retratação: eu tinha sugerido o `read_quotation_list` disparar um `GET /health` para
aquecer. Com o cron já existente, isso é redundante em horário comercial. Continua
valendo **fora** dele — uma cotação pedida às 22h ainda paga os 30-50 s de cold start.
Decidam se o WhatsApp noturno justifica esticar a janela do cron ou aceitar a espera.

### Achado: o expurgo do DANFE nunca rodou

O `README.md` lista, na seção de operação, "Expurgo de PDFs antigos | diário |
`expurgar_antigos(7)`", e a mitigação 3 do risco de link permanente aparece com status
"agendar". Mas as rotas do serviço eram `/danfe`, `/pregerar` e `/health` — **nenhuma
chegava na função**. A mitigação não estava só não agendada: estava inalcançável por
cron. Consistente com o que a pasta mostra: os PDFs em `/public/danfe` são de junho,
julho e agosto, nada foi apagado.

Corrigido em 03/09 com duas rotas, ambas autenticadas e ambas inertes até alguém chamar:

| Rota | Pasta | Padrão |
|---|---|---|
| `POST /expurgo?dias=7` | `/public/danfe` | 7 dias |
| `POST /cotacao/expurgo?dias=30` | `COTACAO_BASE_DIR` | 30 dias |

Para ligar o expurgo do DANFE, que é intenção documentada de vocês, é um cron novo. No
formulário do cron-job.org, campo por campo:

| Campo | Valor |
|---|---|
| Title | `bridge-danfe expurgo danfe` |
| URL | `https://bridge-danfe.onrender.com/expurgo?dias=7` |
| Execution schedule | *Every day at* `03:00` — fora do horário de atendimento |
| **Request method** | **POST** — o padrão do formulário é GET, e a rota só aceita POST (GET devolve 405) |
| Headers | `Authorization` = `Bearer <BRIDGE_TOKEN>` |
| Save responses in job history | ligado, para ver o `{"apagados": N}` |
| Notify me when | execução falha |
| Timeout | o máximo permitido: o expurgo abre uma conexão SFTP e apaga em série |

Dois enganos fáceis nesse formulário: marcar **Requires HTTP authentication** (é Basic
auth, não é o Bearer que a rota espera — o token vai em *Headers*) e deixar o método em
GET.

A rota da cotação **fica sem cron** por decisão de 03/09 — a validade está impressa no
documento. Ela existe para que isso continue sendo uma decisão reversível.

---

## Depois disso

Falta o lado da tool: o `quote_items` chama `POST /cotacao` depois do broadcast do texto e
devolve `AttachmentResponse(data={"attachments": [f"application/pdf:{url}"]})`. O token vai
em `credentials` do `agent_definition.yaml` — **nunca** em `.env` dentro da pasta da tool,
porque o packager do weni-cli sobe a pasta inteira para o Lambda.

Duas coisas continuam **não verificadas** e só o primeiro teste real responde:

- se o egress das tools da VTEX CX alcança `onrender.com`;
- se o anexo é buscado pelo Flows a partir da URL — se for, `arquivos.bisturi.com.br`
  precisa estar acessível para ele, não só para o Lambda.
