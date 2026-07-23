# Integração Garmin — Treinos por Telegram

Cria, agenda e envia treinos para o Garmin Connect (e o relógio) a partir de linguagem natural. O canal principal é o **Telegram**; há também uma UI web mínima para setup/teste.

> Exemplo: você manda `10x300m` ou `rodagem 25min` no Telegram → o bot monta o workout, pergunta o dia, confirma e envia pro Fenix.

---

## Visão geral

| Camada | Função |
|--------|--------|
| **Telegram** | UI do dia a dia (menu, autenticação, fluxo do treino) |
| **Groq (LLM)** | Texto livre → JSON de workout Garmin |
| **Garmin Auth** | SSO mobile + MFA uma vez; refresh automático de tokens |
| **Garmin Connect API** | Criar treino → agendar → mandar pro device |
| **FastAPI** | API + página `/setup` + sobe o bot no mesmo processo |
| **Docker** | Deploy com volume persistente para tokens |

```text
  Telegram / Web
        │
        ▼
   FastAPI + Bot
        │
   ┌────┼────┐
   ▼    ▼    ▼
 Groq  Auth  Connect API
              │
              ▼
         Relógio (fila → sync)
```

---

## Funcionalidades

- **Texto → treino**: intervalado (`10x300m`, `8x400m com 90s`) **ou** contínuo (`rodagem 25min`, `regenerativo 8km`, `rodagem de 20 pace 4:30-4:50`); aquecimento/desaquecimento só se você pedir
- **Fluxo conversacional no Telegram**: rascunho → data (Hoje/Amanhã) → confirmação → envio
- **Auth Garmin sem MFA todo dia**: login mobile SSO + MFA; access/refresh tokens em disco
- **Credenciais por chat**: no Telegram o login fica associado ao `chat_id` (depois costuma pedir só MFA)
- **Envio ao device**: workout FIT na fila do Garmin Connect (`device-service`)
- **API REST** e página web para smoke test
- **Rate limit SSO (429)** tratado com cooldown e mensagens claras

---

## Stack

- Python 3.12, FastAPI, Uvicorn
- `python-telegram-bot` (polling)
- Groq via API compatível OpenAI
- `curl_cffi` (SSO / Cloudflare) + `httpx` (Connect API)
- Docker Compose (DNS público para evitar falhas de resolução)

---

## Requisitos

- Docker + Docker Compose *(recomendado)*  
  **ou** Python 3.12+ local
- Conta [Garmin Connect](https://connect.garmin.com) com MFA
- [Chave Groq](https://console.groq.com/)
- Bot Telegram ([@BotFather](https://t.me/BotFather)) e seu `chat_id`

---

## Subir com Docker

```bash
git clone https://github.com/andreyivanovski/integraca-garmin-telegram.git
cd integraca-garmin-telegram

cp .env.example .env
# edite GROQ_API_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_CHAT_IDS
# e, se quiser, DEFAULT_DEVICE_ID

docker compose up --build -d
```

| Recurso | URL / uso |
|---------|-----------|
| Health | http://localhost:8000/health |
| Setup Garmin | http://localhost:8000/setup |
| Teste web | http://localhost:8000/ |
| Status API | http://localhost:8000/api/status |

Tokens e login do Telegram ficam no volume `garmin_data` → `/data`.

---

## Variáveis de ambiente

Copie de `.env.example`:

| Variável | Obrigatória | Descrição |
|----------|-------------|-----------|
| `GROQ_API_KEY` | sim | API key Groq |
| `GROQ_MODEL` | não | Default `llama-3.3-70b-versatile` |
| `TELEGRAM_BOT_TOKEN` | sim* | Token do BotFather (*sem token o bot não sobe) |
| `TELEGRAM_ALLOWED_CHAT_IDS` | sim* | IDs liberados, separados por vírgula |
| `DEFAULT_DEVICE_ID` | recomendado | ID do seu relógio no Garmin Connect |
| `GARMIN_EMAIL` / `GARMIN_PASSWORD` | recomendado | Login Garmin nos chats liberados (Telegram não pede senha) |
| `DATA_DIR` | não | Pasta de dados (`/data` no Docker) |
| `GARMIN_EMAIL` / `GARMIN_PASSWORD` | não | Só preenche o form de `/setup` |

> Não commite `.env`, tokens nem HARs com credenciais.

---

## Autenticação Garmin

### Opção A — Telegram (uso diário)

1. Coloque `GARMIN_EMAIL` e `GARMIN_PASSWORD` no `.env` (mesmo do container)
2. Seu chat deve estar em `TELEGRAM_ALLOWED_CHAT_IDS`
3. Mande **oi** ou toque em **Reconectar** / `/login`
4. Cole o código MFA quando pedir
5. Pronto: na próxima reconexão o bot usa o `.env` e costuma pedir só MFA

Override opcional: `/creds` salva outro email/senha **só neste chat**.

### Opção B — Web `/setup`

1. Abra http://localhost:8000/setup  
2. Login (fluxo **mobile SSO**, evita CAPTCHA do portal web)  
3. Se pedir MFA, confirme  
4. Confira/ajuste o `deviceId`

Tokens: `/data/garmin_tokens.json`  
- Access token ~24h  
- Refresh ~30 dias (renovado automaticamente nas chamadas à API)

### Rate limit (HTTP 429)

A SSO da Garmin limita tentativas. Se aparecer 429:

- **Espere ~15–20 min** — não fique apertando Reconectar  
- Se **Status** já mostrar conectado, mande o treino direto  
- O app grava um cooldown local para não martelar o SSO

---

## Telegram

### Menu

| Ação | Como |
|------|------|
| Iniciar | `oi`, `olá`, `bom dia`, botão **Novo treino**, `/start` |
| Status | botão **Status** ou `/status` |
| Login | botão **Reconectar**, `/login` |
| Atualizar senha | `/creds` |
| Apagar sessão + login do chat | `/logout` |
| Cancelar fluxo | `/cancel` |

### Fluxo de treino

```text
você:  10x300m
       (ou: rodagem 25min / regenerativo 8km / rodagem de 20 pace 4:30-4:50)
bot:   rascunho do workout
bot:   [Hoje] [Amanhã]
você:  (escolhe o dia)
bot:   confirma?
você:  Confirmar
bot:   criado + agendado + enviado → sync o relógio
```

Também dá para mandar a data em texto (`amanhã`, `15/07/2026`).

### Descobrir o chat_id

Fale com o bot e veja os logs do container, ou use um bot tipo `@userinfobot`, e coloque em:

```env
TELEGRAM_ALLOWED_CHAT_IDS=123456789
```

---

## API (teste)

```http
GET  /health
GET  /api/status

POST /api/draft-workout
Content-Type: application/json
{"text": "10x300m"}

POST /api/execute
Content-Type: application/json
{
  "workout_body": { ... },
  "date": "2026-07-20",
  "device_id": 1234567890
}
```

`execute` exige sessão Garmin já autenticada (`/setup` ou Telegram).

---

## Dev local (sem Docker)

```bash
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
# source .venv/bin/activate

pip install -r requirements.txt
cp .env.example .env   # edite as chaves

uvicorn app.main:app --reload --port 8000
```

Dados locais em `DATA_DIR` (default `data/`).

---

## Estrutura do projeto

```text
app/
  main.py                 # FastAPI + lifespan do bot
  config.py               # Settings (.env)
  auth/garmin_auth.py     # SSO mobile, MFA, tokens, refresh, cooldown 429
  garmin/
    client.py             # create / schedule / send / devices
    workout_schema.py     # validação do body Garmin
  llm/workout_body.py     # texto → JSON (intervalado NxYm + rodagem/regenerativo)
  services/workout_flow.py
  telegram/
    bot.py                # conversa, menu, login por chat
    credentials.py        # email/senha por chat_id
    date_parse.py
  templates/              # HTML /setup e teste
docker-compose.yml
Dockerfile
.env.example
```

---

## Observações importantes

1. **Envio ao relógio é assíncrono**: o Connect coloca na fila; o device baixa na próxima sincronização (app / Wi‑Fi).
2. **Ticket da URL do browser** (`ST-...`) costuma ser *one-time* e já consumido após o redirect — prefira login mobile + MFA no app.
3. **Warm-up / cool-down** só entram se o texto pedir.
4. **Recovery padrão** entre reps: volta (`lap.button`), salvo se o texto especificar outra pausa.
5. **Cloudflare / CAPTCHA** no portal web: o fluxo oficial do app usa a API **mobile** (`GCM_ANDROID_DARK`).
6. Relógio: configure `DEFAULT_DEVICE_ID` no `.env` ou salve em `/setup` (cada pessoa usa o ID do próprio device).

---

## Segurança

- `.env`, `data/` e tokens **não devem** ir para o Git  
- Restrinja o bot com `TELEGRAM_ALLOWED_CHAT_IDS`  
- Credenciais Garmin no Telegram ficam no volume (`telegram_credentials.json`) — trate o volume como secreto  
- Rotacione tokens se algum arquivo/log tiver vazado em ambientes compartilhados  

---

## Licença / uso

Projeto pessoal de integração com APIs não oficiais / reverse-engineered do Garmin Connect. Use por sua conta e risco; a Garmin pode alterar endpoints, rate limits ou termos a qualquer momento.

---

## Custo / consumo (container pago)

O que mais consome com o app **ocioso**:

1. **Telegram polling** — o bot fica fazendo `getUpdates` o tempo todo.
2. **Duas instâncias do mesmo bot** — gera `Conflict` e retry em loop. Só 1 réplica; pare o Docker local se o bot estiver na nuvem.
3. **Healthcheck muito frequente** — no painel do host, use intervalo ≥ 30–60s.

Para uso raro (~1x/dia), a forma mais barata sem mudar o app: **ligar o container só na hora** e desligar depois.

O que **não** gasta parado: Groq, login Garmin, criar treino (só sob demanda).

---

## Atalhos úteis

```bash
docker compose up --build -d
docker compose logs -f web
docker compose restart
docker compose down
```
