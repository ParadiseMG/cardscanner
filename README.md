# CardScanner

A desktop dashboard that turns "I have 3,000 cards in shoeboxes" into a structured, valued, eBay-ready inventory — automatically.

Built for Connor's Danu workflow. Photos go into a Google Drive folder; the dashboard pulls them, identifies each card with Claude vision, looks up sold comps on eBay, matches against a Hit Watchlist, and gives you one-click eBay listing creation.

```
Drive inbox  ──►  Claude vision  ──►  eBay sold comps  ──►  SQLite + Sheets + xlsx  ──►  eBay listing
   (you upload)     (auto identify)   (auto price)         (auto save)                  (one click)
```

## Features

- **Drive-as-inbox** — drop photos in `Baseball Cards/To Be Processed/` from your phone or desktop. The dashboard scans, processes, then moves files to `Processed/` (or `Failed/` with metadata) so nothing is processed twice.
- **Claude vision identification** — year, set, player, card #, parallel, condition, autograph/relic detection, with a confidence score. Low-confidence cards are flagged for review.
- **Sold-comp lookup** — scrapes eBay completed/sold listings to compute median, low, high, count.
- **Hit Watchlist matching** — pre-seeded from your existing `Hit Watchlist` tab (Griffey ‘89 UD, Trout RC, Acuna Chrome, vintage stars, autos/relics catch-all). Easily extensible.
- **Gamified dashboard** — 24-achievement catalog, value milestones (`$100 → $500 → $1K → $5K`), daily streak, era/player charts, smart insights ("hit rate from modern: 28%"), confetti on big hits.
- **Action queue** — surfaces "needs review", "consider grading", "needs photo verification" so you always know what to look at next.
- **eBay Sell-API integration** — preview, edit, draft, publish. Auction <$50 / BIN ≥$50 defaults. Bulk "List Selected". Phase-1 ships against eBay Sandbox; flip `EBAY_ENV=production` for real listings.
- **Three-place persistence** — SQLite (fast queries), Google Sheets (your CardScanner Inventory), and append-rows in your existing `Baseball_Cards.xlsx` Inventory tab (failsafe).

## Architecture

```
app/
├── main.py              FastAPI entry, route wiring, static mount
├── config.py            Settings (env-driven, .env supported)
├── db.py                SQLite engine + Hit Watchlist seed
├── models.py            Card, Listing, ScanJob, AchievementUnlock, ...
├── pipeline.py          Async batch + Drive-sync runners
├── achievements.py      Catalog + unlock evaluator + stats/insights
├── routers/             scan, inventory, stats, auth, listings, sync, drive
├── services/
│   ├── claude_vision.py     Claude API call + JSON parsing
│   ├── comp_lookup.py       eBay sold-listings scrape + median
│   ├── hit_watchlist.py     Pattern matcher
│   ├── sheets_sync.py       Google Sheets OAuth + append
│   ├── xlsx_mirror.py       Append to Baseball_Cards.xlsx
│   ├── drive_inbox.py       Drive folder mgmt + image fetch + move
│   └── ebay_listing.py      eBay OAuth + draft + publish
└── static/              index.html (Tailwind+Alpine), app.js, style.css
tests/                   23 pytest tests, no live-network required
```

## Prerequisites

- Python 3.10+
- A Google account (for Drive + Sheets)
- An Anthropic API key (paste in dashboard, stored in browser localStorage only)
- An eBay Developer account — sandbox credentials are instant; production keyset takes 1-3 days

## First-run setup

### 1. Install

```bash
cd ~/Documents/CardScanner
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# edit .env to point LOCAL_XLSX_PATH at your existing Baseball_Cards.xlsx (optional)
```

### 2. Start the server

```bash
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
# or:  python -m app.main
```

Open <http://127.0.0.1:8765>.

### 3. Connect Anthropic (Claude vision)

Click ⚙︎ Settings in the dashboard → paste your key (`sk-ant-…`) → Save.

> **Why a key, not OAuth?** Anthropic restricted OAuth to Claude.ai and Claude Code in their Feb 2026 policy update — third-party apps must use API keys. The key stays in your browser's localStorage; the server only forwards it to api.anthropic.com when needed. (You can also set `ANTHROPIC_API_KEY` in `.env` to share across browsers.)

### 4. Connect Google (Drive + Sheets)

1. Go to <https://console.cloud.google.com/apis/credentials>.
2. Create a project (or pick one), then **Create OAuth client ID** → application type **Desktop app**.
3. Download the JSON, save it to `data/google_client_secret.json`.
4. Enable the Drive API and Sheets API in your project.
5. In the dashboard: ⚙︎ Settings → **Connect Google** → grant access.

The first sync will auto-create:

```
Baseball Cards/
├── To Be Processed/
├── Processed/
└── Failed/
```

If you don't set `GOOGLE_SHEET_ID`, the app will create a "CardScanner Inventory" spreadsheet on first save.

### 5. Connect eBay (sandbox first)

1. Go to <https://developer.ebay.com/my/keys> and create a sandbox keyset (instant).
2. Set up an "RuName" (redirect URL identifier) on the same page, pointing it at `http://127.0.0.1:8765/api/auth/ebay/callback`.
3. Drop these into `.env`:
   ```
   EBAY_ENV=sandbox
   EBAY_APP_ID=...
   EBAY_CERT_ID=...
   EBAY_DEV_ID=...
   EBAY_RUNAME=...
   ```
4. Create one **fulfillment**, **payment**, and **return** policy on eBay Sandbox seller hub (one-time). Copy the IDs into `.env`.
5. In the dashboard: ⚙︎ Settings → **Connect eBay** → grant access.

**Phase 2 (later):** apply for production keyset on eBay (1-3 day approval). Swap `EBAY_ENV=production` and the production keys into `.env`. Same code path.

## Daily workflow

1. Take photos of cards on your phone (the eBay/iOS Camera app's "Document" mode crops nicely).
2. Drag the photos into your Drive `Baseball Cards/To Be Processed/` folder.
   - **Single-sided cards**: any filename, e.g. `IMG_4421.jpg`.
   - **Two-sided cards**: name them `whatever_front.jpg` and `whatever_back.jpg`. The matcher pairs by base name.
3. In the dashboard, click **Sync new cards →** (or enable auto-sync — checks every 5 min).
4. Watch the progress bar; cards start landing in **Recent scans** with their identification, comp price, and Hit Watchlist status.
5. Click any card to edit — Claude isn't always right about parallels.
6. Click the **eBay** button on any row to preview an auto-built listing (title, format, price, item-specifics, fee math), edit, and publish.

## Tests

```bash
source .venv/bin/activate
python -m pytest tests/ -q
```

The 23 tests cover comp parsing, achievement engine, Hit Watchlist matcher, eBay template builder, Drive file pairing, and an end-to-end pipeline test (Claude + comp lookup mocked).

## Design choices, briefly

- **No camera/getUserMedia.** Connor pivoted to batch upload — Drive is the universal inbox, no special UI needed. Drag-drop in the dashboard is kept as a fallback.
- **SQLite as primary store, Sheets/xlsx as views.** SQLite gives instant queries for stats. Google Sheets and the existing `.xlsx` are mirrored for portability (Sheets is the cloud copy you can share; xlsx keeps the existing tracker alive).
- **Anthropic API key, not OAuth.** Documented above — Anthropic's OAuth is now reserved for Claude.ai/Claude Code only.
- **eBay scraping for sold comps.** The Marketplace Insights API gates small developers. The public sold-listings page is publicly indexable; we pull it server-side, parse with BeautifulSoup, cache for an hour.
- **Phase-1 sandbox for eBay.** Sandbox is instant — Connor can verify the entire UX flow today. Phase 2 is a config flip once eBay approves the production keyset.
- **No browser localStorage for app data.** Only the Anthropic key (so it never touches the server). All other state is server-side, easy to reason about.

## Known limitations

- The eBay sold-comp scraper depends on eBay's HTML structure; if they change it the parser needs an update. Cached for an hour to be polite.
- HEIC images aren't auto-converted; convert to JPG/PNG before uploading. (iOS Photos can do this on share.)
- The Drive "make public" trick for listing photos isn't yet wired — the eBay publish path uses an eBay placeholder image in sandbox. Phase 2 should upload via eBay EPS.
- Single-user, single-machine. No login, no multi-tenancy. Add those when productizing for Danu.

## Next-up roadmap (Danu)

- **Bulk-grading recommendations** — flag cards where (raw value × grading-bump) > grading cost.
- **Sales tracking + tax export** — feed the existing Sales Log tab from eBay sold webhooks.
- **Multi-sport** — same pipeline, different category IDs and watchlists.
- **Mobile companion** — the desktop app is the source of truth, but a mobile shortcut to "open Drive folder + camera" would close the loop.
