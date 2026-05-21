# Vision Backend Configuration

CardScanner uses AI vision to identify trading cards from photos. The vision
backend is configurable — you can use a local Ollama model, the Claude API,
or the Claude CLI.

## Backend Selection

Set `VISION_BACKEND` in `.env`:

| Value | Description | When to use |
|---|---|---|
| `auto` | CLI if available, else HTTP (original behavior) | Local development on Mac |
| `ollama` | Local Ollama server, falls back to Claude on failure | Server deployment |
| `http` | Anthropic API directly | When you have an API key and want reliability |
| `cli` | Claude Code CLI (`claude -p`) | Personal use on Mac with Claude Code installed |

## Ollama Backend

### Config

```env
VISION_BACKEND=ollama
OLLAMA_BASE_URL=http://100.71.106.53:11434
OLLAMA_VISION_MODEL=llama3.2-vision:11b
```

### How it works

1. Card images are base64-encoded and sent to Ollama's `/api/chat` endpoint
2. The same prompt used for Claude is sent to the local model
3. The model returns JSON with card identification fields
4. If the model response can't be parsed, it retries up to 3 times with backoff

### Fallback chain

If Ollama fails all 3 retries (server down, model not loaded, timeout):

```
Ollama → Claude CLI (if installed) → Claude HTTP (if API key set) → Error
```

This means a temporary Ollama outage won't break card processing — it
transparently falls back to Claude.

### Accuracy tradeoffs

Local vision models (7-11B params) are less accurate than Claude Sonnet for
card identification. Expect:

- **Good:** Modern base cards, clear photos, common brands (Topps, Panini)
- **Worse:** Vintage cards, parallels (Refractor vs. base), serial numbers,
  condition assessment, card numbers on busy backs
- **More review flags:** `review_flagged` will be `true` more often; check
  the dashboard's "Needs Review" section regularly

If accuracy is unacceptable, switch back with `VISION_BACKEND=auto` or `http`.

### Re-prompts

When a field has low confidence (< 0.5), a single-field re-prompt is sent.
This also goes through Ollama when `VISION_BACKEND=ollama`. Re-prompts are
capped at 2 fields per card to avoid excessive inference time.

## HTTP Backend (Claude API)

### Config

```env
VISION_BACKEND=http
ANTHROPIC_API_KEY=sk-ant-...
ANTHROPIC_MODEL=claude-sonnet-4-6
```

### Notes

- Requires a valid Anthropic API key
- Most accurate option — Claude Sonnet excels at reading card details
- Costs money per card (~$0.01-0.03 per identification depending on image size)
- 30s timeout, 3 retries with exponential backoff

## CLI Backend (Claude Code)

### Config

```env
VISION_BACKEND=cli
# No API key needed — uses your Claude.ai login via Claude Code
```

### Notes

- Requires `claude` CLI installed and on PATH
- Uses your Claude.ai OAuth login (personal/internal use only)
- 60s timeout (subprocess startup adds overhead)
- Not available in Docker — CLI requires an interactive login session

## Testing your backend

Quick check which backend is active:

```bash
curl http://localhost:8765/api/health | jq '.vision_backend'
```

Test a card identification:

```bash
# Upload a card image and check the response
curl -X POST http://localhost:8765/api/scans/upload \
  -F "front=@/path/to/card.jpg"
```

## Switching backends

1. Edit `.env` and change `VISION_BACKEND`
2. Restart: `docker compose restart` (or kill/restart uvicorn locally)
3. No database migration needed — the backend only affects the identification
   step, not the stored data
