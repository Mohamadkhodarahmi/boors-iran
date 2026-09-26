# Cloudflare Worker — API + Telegram bot

The Worker only stores and serves results. All data collection and analysis happens on the local
machine (or VPS), which pushes results to `POST /api/ingest/*`.

## One-time setup

```bash
cd worker
npm install
npx wrangler login
npx wrangler d1 create bors            # copy the printed database_id into wrangler.jsonc
npx wrangler d1 migrations apply bors --remote

# secrets (never put them in wrangler.jsonc or code)
npx wrangler secret put INGEST_TOKEN             # long random string; same value as BORS_INGEST_TOKEN in ../.env
npx wrangler secret put TELEGRAM_BOT_TOKEN       # from @BotFather
npx wrangler secret put TELEGRAM_WEBHOOK_SECRET  # random string, letters/digits/_/- only
npx wrangler secret put READ_TOKEN               # optional: protects the GET API

npx wrangler deploy
```

Set `ADMIN_IDS` (your numeric Telegram id) and optionally `ACCESS_MODE=allowlist` in `wrangler.jsonc` → `vars`.

Register the webhook (through your proxy if Telegram is filtered):

```bash
curl "https://api.telegram.org/bot<TOKEN>/setWebhook" \
  -d "url=https://bors-analyser.<subdomain>.workers.dev/telegram/webhook" \
  -d "secret_token=<TELEGRAM_WEBHOOK_SECRET>" \
  -d 'allowed_updates=["message"]'
```

## Endpoints

| Method | Path | Auth |
|---|---|---|
| POST | `/api/ingest/analysis` · `/market` · `/signals` · `/codal` | `Authorization: Bearer INGEST_TOKEN` |
| POST | `/telegram/webhook` | `X-Telegram-Bot-Api-Secret-Token` |
| GET | `/market` `/stocks` `/stock/{symbol}` `/analysis/{symbol}?mode=` `/signals` `/tablo/{symbol}` `/technical/{symbol}` `/fundamental/{symbol}` | `READ_TOKEN` if set |

Bot commands: `/start /help /market /top /scan /stock /tablo /technical /fundamental`, a bare symbol
works like `/stock`. Admin: `/stats /approve <id> /block <id> /unblock <id>`.

## Local testing

```bash
printf 'INGEST_TOKEN=local-test-token\nTELEGRAM_BOT_TOKEN=000:fake\nTELEGRAM_WEBHOOK_SECRET=local-hook-secret\n' > .dev.vars
npx wrangler d1 migrations apply bors --local
npx wrangler dev --port 8787
# in the project root:
BORS_WORKER_URL=http://127.0.0.1:8787 BORS_INGEST_TOKEN=local-test-token uv run python -m bors_analyser publish
```
