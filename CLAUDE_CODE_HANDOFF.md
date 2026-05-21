# CardScanner — Handoff to Claude Code

**Date:** 2026-05-11
**Picking up from:** Cowork session, post-R4 + comp-lookup work
**Working dir:** `~/Documents/CardScanner/`

---

## TL;DR — One paragraph

Comp lookup is returning $0 because eBay hard-blocks plain `httpx` with HTTP 403 (Cloudflare). The code now has a 3-tier fallback chain (Browse API → Playwright → httpx), but the running uvicorn is on stale code and may also be missing the Chromium browser binary. Today's fix: install chromium, hard-restart uvicorn, verify the diag endpoint returns `fetched_via: "playwright"` with real prices. Tomorrow's permanent fix: drop production eBay app keys in `.env`, set `EBAY_ENV=production`, restart, and the Browse API takes over.

---

## Suggested first prompt for Claude Code

> Comp lookup is returning 403 from eBay. Playwright is in `requirements.txt` and (Connor says) installed in `.venv`, but the running uvicorn doesn't seem to be using it — diag responses are missing the `fetched_via` field that `app/routers/diag.py` adds. Get `http://127.0.0.1:8765/api/diag/comp-probe?year=1990&set_brand=Topps&player=Dwight+Evans` returning real prices via Playwright. Install the chromium binary if missing, hard-restart uvicorn (kill the PID — don't rely on `--reload`), then verify. The intended fallback chain is Browse API → Playwright → httpx. Don't come back to me until it works or you genuinely need me.

---

## Repro steps for the Claude Code agent

1. **Verify Playwright python package**
   ```bash
   .venv/bin/pip show playwright
   ```

2. **Verify Chromium binary is present** (this is what's likely missing)
   ```bash
   ls -la ~/Library/Caches/ms-playwright/ 2>/dev/null || \
     .venv/bin/python3 -m playwright install chromium
   ```

3. **Find and kill the running uvicorn** (don't rely on `--reload`)
   ```bash
   lsof -i :8765 | grep LISTEN
   kill <PID>
   ```

4. **Restart uvicorn cleanly**
   ```bash
   cd ~/Documents/CardScanner
   source .venv/bin/activate
   uvicorn app.main:app --host 127.0.0.1 --port 8765 --reload
   ```
   (Connor has this aliased as `cs` — confirm with `alias cs` in his shell.)

5. **Clear the FAIL cache** (5-min cache will keep returning empty otherwise)
   ```bash
   curl -X POST http://127.0.0.1:8765/api/diag/clear-comp-cache
   ```

6. **Hit the probe**
   ```bash
   curl 'http://127.0.0.1:8765/api/diag/comp-probe?year=1990&set_brand=Topps&player=Dwight+Evans' | jq
   ```
   **Success looks like:**
   ```json
   {
     "fetched_via": "playwright",
     "status": 200,
     "html_length": 250000,
     "selector_hits": {...},
     "extracted_prices_count": 30,
     "first_5_prices": [3.99, 4.50, ...]
   }
   ```
   **Still broken if** `fetched_via` is absent (= old code) or `"httpx_failed"` (= playwright never ran).

7. **If playwright still doesn't run**, check uvicorn console for these log lines from `app/services/browser_fetch.py`:
   - `playwright launch failed` → browser binary problem, re-run `playwright install chromium`
   - `playwright not importable` → python pkg actually missing, `pip install playwright`
   - No log lines at all → `is_available()` returning False; check the import guard at top of `browser_fetch.py`

---

## Architecture cheat-sheet

### Comp lookup chain (`app/services/comp_lookup.py`)

```
fetch_comps(query)
  ├── PATH 1: ebay_browse.fetch_active_listings(query)     ← needs prod keys
  │     suggest_price = min(active) × (1 - 0.06)           ← 6% undercut
  │
  ├── PATH 2: browser_fetch.fetch_html(ebay_search_url)    ← Playwright
  │     _parse_prices(html)  ← DOM selectors + text-sweep fallback
  │
  └── PATH 3: httpx.get(ebay_search_url)                   ← always 403's
```

### Key files I touched (uncommitted, working tree)

- `app/services/comp_lookup.py` — 3-tier chain wiring
- `app/services/browser_fetch.py` — **new**, Playwright singleton
- `app/services/ebay_browse.py` — **new**, Browse API client
- `app/services/auto_pair.py` — **new**, EXIF-time pairing + verification
- `app/routers/diag.py` — `/api/diag/comp-probe` and `/api/diag/clear-comp-cache`
- `app/routers/inventory.py` — `POST /api/inventory/dedupe`
- `app/routers/scan.py` — `/api/scans/recent-failures`, `/api/scans/jobs/{id}/failures`
- `app/routers/drive.py` — `POST /api/drive/retry-failed`
- `app/pipeline.py` — `_record_failure()`, drive-sync lock, EXIF auto-pair wiring
- `app/services/drive_inbox.py` — added `image/heif` to `IMAGE_MIMES`
- `app/services/claude_vision.py` — CLI backend via `claude -p` subprocess
- `app/static/index.html` + `app.js` — Recent failures panel
- `setup_extras.sh` — **new**, one-shot Playwright installer
- `requirements.txt` — added `pillow-heif`, `piexif`, `playwright==1.47.0`
- `.env.example` — added `PRICING_UNDERCUT_PCT=6.0`, eBay env vars
- `app/config.py` — added `pricing_undercut_pct`

### `.env` — what's set vs what's missing

Set:
- `ANTHROPIC_API_KEY` (or use CLI backend via `claude -p`)
- `GOOGLE_OAUTH_CLIENT_SECRETS`, `GOOGLE_SHEET_ID`
- `LOCAL_XLSX_PATH`
- eBay sandbox keys: `EBAY_APP_ID`, `EBAY_CERT_ID`, `EBAY_DEV_ID`, `EBAY_RUNAME`
- `EBAY_ENV=sandbox`

**Missing — fill in tomorrow when production keys arrive:**
- `EBAY_APP_ID` / `EBAY_CERT_ID` (production keyset, replaces sandbox)
- Flip `EBAY_ENV=production`
- (Production keys = Browse API works, no scraping needed)

---

## After comp lookup is fixed — cleanup punchlist

```bash
# 1. Clear stale FAIL cache
curl -X POST http://127.0.0.1:8765/api/diag/clear-comp-cache

# 2. Dedupe the 11 race-duplicates from earlier
curl -X POST http://127.0.0.1:8765/api/inventory/dedupe

# 3. Move the 4 failed JPEGs back to inbox (or click "Retry failed in Drive" on dashboard)
curl -X POST http://127.0.0.1:8765/api/drive/retry-failed

# 4. Click Sync on dashboard to reprocess
```

---

## Known sharp edges

- **Don't trust `--reload`** — it sometimes misses brand-new module imports (e.g. `browser_fetch.py`). Hard-restart on new files.
- **Don't `pip install playwright` then forget chromium** — the python pkg ships without browsers; `playwright install chromium` is a separate ~150 MB download.
- **eBay returns 403 to ALL httpx scrapes** — verified against eBay, 130point, sportscardspro, cardladder, COMC, PSA. Real browser is the only path.
- **Drive-sync has an in-flight lock** in `pipeline.run_drive_sync` — second concurrent sync is refused, not queued. By design.
- **Schema is at migration #9** (job failures table). If something complains about missing tables, check `app/db.py` migration list.
- **HEIF fix already in** — `image/heif` MIME is now in `drive_inbox.IMAGE_MIMES` allowlist (was previously only `image/heic`, which is why Carleigh's 38 photos were ignored).
- **Stale FUSE-mount git lockfiles** sometimes block commits inside the sandbox; on the Mac just `rm -f .git/*.lock` if you hit it.

---

## Tomorrow's prompt (when eBay prod keys arrive)

> eBay just sent my production app keyset. Drop them in `.env`, replace `EBAY_ENV=sandbox` with `EBAY_ENV=production`, restart uvicorn, and verify `/api/diag/comp-probe?year=1990&set_brand=Topps&player=Dwight+Evans` returns `fetched_via: "browse_api"` with `suggested = min(active) × 0.94`. Then run `POST /api/diag/clear-comp-cache` and `POST /api/inventory/dedupe`.

---

## Connor's preferences (from this session)

- Single-quote URLs in shell commands (`&` interpreted as backgrounding otherwise)
- Use up-arrow recall — don't make him retype long uvicorn commands
- Distinguish "open in browser" vs "run in shell" when sending links
- Dashboard buttons > curl commands when possible
- He has `cs` aliased for the uvicorn restart
- Keep me focused: "only come back to me when you're sure it works or when you absolutely need me"
