# CardScanner — Server Deployment Guide

CardScanner runs as a Docker container on the home server, with Ollama providing
local AI vision for card identification.

## Architecture Overview

```
┌─────────────────────────────────────────────────────────┐
│  Home Server (100.71.106.53 via Tailscale)              │
│                                                         │
│  ┌─────────────────────┐   ┌──────────────────────┐     │
│  │  CardScanner Docker │   │  Ollama (port 11434) │     │
│  │  (port 8765)        │──▶│  llama3.2-vision:11b │     │
│  │                     │   │  (GTX 1070 GPU)      │     │
│  │  FastAPI + SQLite   │   └──────────────────────┘     │
│  │  Playwright headless│                                │
│  └────────┬────────────┘                                │
│           │ /data volume                                │
│           ▼                                             │
│  ┌────────────────────┐                                 │
│  │  cardscanner.db    │                                 │
│  │  google_token.json │                                 │
│  │  ebay_token.json   │                                 │
│  │  uploads/          │                                 │
│  └────────────────────┘                                 │
└─────────────────────────────────────────────────────────┘
         ▲
         │ Tailscale / LAN
         │
┌────────┴────────┐
│  Connor's Mac   │
│  Browser → :8765│
│  Phone (ntfy)   │
└─────────────────┘
```

## Prerequisites

- Docker + Docker Compose on the server
- Ollama installed with a vision model pulled
- Tailscale (or LAN access) between your Mac and the server

## First-Time Setup

### 1. Pull the vision model on the server

```bash
ssh your-server
ollama pull llama3.2-vision:11b
```

Verify it's ready:

```bash
curl http://localhost:11434/api/tags | jq '.models[].name'
```

### 2. Clone the repo on the server

```bash
git clone <your-repo-url> ~/CardScanner
cd ~/CardScanner
```

### 3. Copy data from your Mac

These files contain OAuth tokens and the database — they don't exist in git.

```bash
# From your Mac:
scp -r ~/Documents/CardScanner/data/ your-server:~/CardScanner/data/
```

The critical files in `data/`:
- `cardscanner.db` — the SQLite database (all cards, jobs, watchlist)
- `google_client_secret.json` — Google OAuth client credentials
- `google_token.json` — Google OAuth refresh token (created after first auth)
- `ebay_token.json` — eBay OAuth token
- `uploads/` — all card images

### 4. Create `.env`

```bash
cp .env.example .env
```

Edit `.env` and set at minimum:

```env
# Use Ollama for vision instead of Claude API
VISION_BACKEND=ollama
OLLAMA_BASE_URL=http://host.docker.internal:11434
# If Ollama is on the same machine but outside Docker, use the host IP
# or host.docker.internal. If Ollama is also in Docker, use the container name.

# Push notifications for captchas
NTFY_TOPIC=cardscanner-connor

# Copy these from your Mac's .env
ANTHROPIC_API_KEY=sk-ant-...        # fallback if Ollama fails
GOOGLE_SHEET_ID=...
EBAY_APP_ID=...
EBAY_CERT_ID=...
EBAY_DEV_ID=...
EBAY_ENV=sandbox                    # or production
```

### 5. Set up ntfy on your phone

1. Install the [ntfy app](https://ntfy.sh) (iOS or Android)
2. Subscribe to topic: `cardscanner-connor` (or whatever you set in `NTFY_TOPIC`)
3. Enable notifications — you'll get a push with a screenshot when a captcha hits

### 6. Start the container

```bash
cd ~/CardScanner
docker compose up -d
```

Verify it's running:

```bash
docker compose logs -f          # watch startup logs
curl http://localhost:8765/api/health   # health check
```

Access the dashboard at `http://100.71.106.53:8765` from any device on Tailscale.

## Updating

```bash
cd ~/CardScanner
git pull
docker compose up -d --build
```

The `/data` volume is persistent — rebuilding the container does not lose your
database, tokens, or uploads.

## Managing Ollama Models

```bash
# List installed models
curl http://localhost:11434/api/tags | jq '.models[].name'

# Pull a new/updated model
ollama pull llama3.2-vision:11b

# Try a different vision model (update .env afterward)
ollama pull llava:7b
# Then set OLLAMA_VISION_MODEL=llava:7b in .env and restart
```

### Model options for the GTX 1070 (8GB VRAM)

| Model | VRAM | Quality | Speed |
|---|---|---|---|
| `llama3.2-vision:11b` | ~7GB | Best | ~10-15 tok/s |
| `minicpm-v` | ~5GB | Good | ~15 tok/s |
| `llava:7b` | ~4.5GB | OK | ~20 tok/s |
| `moondream2` | ~1.7GB | Basic | ~40 tok/s |

If card identification accuracy is poor, try a larger model or fall back to Claude
by setting `VISION_BACKEND=auto` (uses Anthropic API with `ANTHROPIC_API_KEY`).

## Common Operations

### Trigger a Drive sync

```bash
curl -X POST http://localhost:8765/api/drive/sync
```

Or click "Sync" on the dashboard.

### Check if a captcha is waiting

```bash
curl http://localhost:8765/captcha/status
```

Or visit `http://100.71.106.53:8765/captcha` in a browser.

### View logs

```bash
docker compose logs -f --tail 100
```

### Restart

```bash
docker compose restart
```

### Full rebuild (after code changes)

```bash
docker compose up -d --build
```

## Troubleshooting

### "Ollama failed and no Claude fallback available"

Ollama isn't reachable and no `ANTHROPIC_API_KEY` is set for fallback.

1. Check Ollama is running: `curl http://localhost:11434/api/tags`
2. Check the URL in `.env` matches how Docker reaches the host
3. If Ollama is on the same machine, try `OLLAMA_BASE_URL=http://host.docker.internal:11434`
4. As a quick fix, set `VISION_BACKEND=auto` and ensure `ANTHROPIC_API_KEY` is set

### Google OAuth "token expired"

The refresh token in `google_token.json` lasts indefinitely, but Google can
revoke it if you change your password or security settings.

To re-auth:
1. Run CardScanner locally on your Mac (you need a browser for the OAuth flow)
2. Complete the Google OAuth flow
3. Copy the new `data/google_token.json` to the server's data volume

### Captcha not solvable / ntfy not working

1. Check `NTFY_TOPIC` is set in `.env`
2. Check you're subscribed to the same topic in the ntfy app
3. Visit `http://100.71.106.53:8765/captcha` directly — the page auto-refreshes
4. If captchas are frequent, consider getting eBay production API keys (Browse API
   bypasses scraping entirely)

### Container won't start / missing tables

Check if migrations ran:

```bash
docker compose logs | grep -i migration
```

Migrations run automatically on startup. If the database file is missing or corrupt,
restore from your Mac's backup in `data/cardscanner.db`.

### Uploads not persisting

Make sure the Docker volume is mounted correctly. Check with:

```bash
docker compose exec cardscanner ls -la /data/
```

The `/data` directory should contain `cardscanner.db`, token files, and `uploads/`.
