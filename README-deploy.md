# Deploy da bridge no Render (plano free)

## Arquivos deste pacote

| Arquivo | Para quê |
|---|---|
| `bridge_danfe.py` | o serviço |
| `requirements.txt` | dependências, nas versões efetivamente testadas |
| `render.yaml` | configuração do Render (Blueprint) |
| `.htaccess` | vai no **Umbler**, não aqui — ver passo 0 |

## Passo 0 — antes de tudo, no Umbler

Suba o `.htaccess` em `/public_html/danfe/`. Sem `Options -Indexes`, qualquer
pessoa abre `arquivos.bisturi.com.br/danfe/` e vê a lista de todas as notas.
Apague também o `371006.pdf` de teste (nome sequencial, adivinhável).

## Passo 1 — repositório

Suba `bridge_danfe.py`, `requirements.txt` e `render.yaml` num repositório Git
(GitHub/GitLab). **Nenhuma credencial vai no repositório** — todas entram como
variável na interface do Render.

## Passo 2 — criar o serviço

No Render: **New → Blueprint** → aponte para o repositório. Ele lê o
`render.yaml` e pede os 5 valores marcados como secretos:

| Variável | Valor |
|---|---|
| `BRIDGE_TOKEN` | invente um token forte — é o que o Code Action enviará |
| `SFTP_PASSWORD` | senha do usuário `umbler` |
| `VTEX_ACCOUNT` | nome da conta VTEX (o que aparece na URL do admin) |
| `VTEX_APP_KEY` | appKey gerada na VTEX |
| `VTEX_APP_TOKEN` | appToken gerado na VTEX |

Confirme o `SFTP_BASE_DIR`: está como `/public_html/danfe`. Se ao conectar por
FileZilla os arquivos do site estiverem em outra pasta, ajuste a variável.

## Passo 3 — validar

O Render dá uma URL tipo `https://bridge-danfe.onrender.com`.

```bash
# 1. serviço no ar
curl https://bridge-danfe.onrender.com/health
# -> {"status":"ok"}

# 2. ponta a ponta, com um pedido que sabemos ter nota
curl -X POST https://bridge-danfe.onrender.com/danfe \
  -H "Authorization: Bearer $BRIDGE_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"orderId":"1600000000000-01"}'
# -> {"orderId":"...","notas":[{"numero":"...","serie":"50",
#     "chave":"...","pdf_url":"https://arquivos.bisturi.com.br/danfe/...pdf"}]}
```

Abra a `pdf_url` no navegador. Se o DANFE aparecer, todo o risco técnico
acabou — o que resta é integração de fluxo na Weni.

## ⚠️ Hibernação — leia antes de ir para produção

O plano free do Render **hiberna o serviço após ~15 min sem tráfego**. A
primeira chamada depois disso demora de 30 a 50 segundos para responder,
porque o container precisa subir.

Para atendimento isso é ruim: o cliente pede a nota e fica esperando quase um
minuto sem entender por quê. Três formas de lidar:

1. **Ping periódico** (mais simples): cadastre `GET /health` num cron externo
   gratuito, tipo cron-job.org, a cada 10 minutos. Mantém acordado durante o
   horário comercial. Note que isso consome as horas mensais gratuitas do
   Render, então prefira agendar só no horário de atendimento.
2. **Mensagem de espera no fluxo**: o agente responde "só um instante, estou
   buscando sua nota" antes de chamar o Code Action. Não resolve a lentidão,
   mas evita a sensação de travamento. Vale fazer de qualquer forma.
3. **Migrar quando incomodar**: Cloud Run não tem esse problema e a cota
   gratuita cobre folgadamente esse volume. O `bridge_danfe.py` roda igual
   nos dois — só o passo de deploy muda.

Sobre timeout: gerar o PDF leva ~0,13s, então o processamento em si nunca é o
gargalo. O único atraso relevante é o cold start.

## Manutenção

Agende `expurgar_antigos(7)` para rodar por cron. É o que compensa o link do
Umbler não expirar: reduz a janela em que cada nota fica acessível. Se o
cliente pedir a nota depois, a bridge regenera na hora.
