# Round 4 — Closing the loop

R1 made it work. R2 made it scale. R3 made it smart. R4 makes it **pay** — turning the catalog into actual realized revenue and surfacing the financial picture Connor's Baseball_Cards.xlsx Sales Log was originally meant to track.

**Premise:** the dashboard currently identifies, prices, and drafts listings — but the loop closes off-app. The cards sell on eBay, the money lands in Connor's account, and nothing in CardScanner reflects it. Round 4 brings the sales side back into the dashboard and lights up two adjacent workflows: PSA-style grading submissions and intelligent bulk-lot clustering (the "what do I do with 1,500 sub-dollar 1989 Donruss commons?" problem).

**Three themes:**
1. **Sales tracking** — pull eBay listing state transitions back into the dashboard (active → sold), capture realized prices, build a real P&L view.
2. **Grading submission** — the R3 Grade suggestion currently dismisses to a "submit manually" message. R4 builds the actual submission packet (PSA's CSV upload format is standard) and wires the flow.
3. **Bulk-lot clustering** — auto-group sub-dollar singles by era/set into proposed lots, with title/price suggestions for posting them.

**Plus loose ends from R3:**
- Wire the server-side `POST /api/inventory/{id}/reidentify` endpoint the R3 frontend already calls.
- New `GET /api/stats/last_24h` so the activity stripe doesn't have to derive client-side.
- Server-side `comp_confidence` filter on `/api/inventory` (currently client-side per R3 TODO).

**Out of scope for Round 4:** multi-user auth, cloud deploy, sports beyond baseball, photo background removal. Round 5 candidates.

---

## Workstream A — Sales tracking + P&L

Owner files: `app/services/ebay_listing.py`, `app/services/sales_sync.py` (new), `app/routers/sales.py` (new), `app/routers/listings.py` (mark-sold path expansion), `app/models.py` (additive only — see field list), `app/migrations.py` (id=7), `tests/test_sales_*.py`. May lightly touch `app/routers/inventory.py` only to expose new sale-status fields in `CardOut`.

### A1. Pull eBay listing state via the Sell API

New service `app/services/sales_sync.py`:

- `async def fetch_listing_status(offer_id: str) -> dict` — calls `/sell/inventory/v1/offer/{offer_id}` and returns `{status, listing_id, current_price, sold_price?, sold_at?}`. Maps eBay's offer statuses (`UNPUBLISHED`, `PUBLISHED`, `ENDED`) plus the publish-result `listingId` into our internal `Listing.status` vocabulary (`draft`/`active`/`sold`/`ended`).
- `async def sync_listing(listing: Listing) -> bool` — pulls latest state, updates the DB. Detect `sold` by checking the order via `/sell/fulfillment/v1/order?filter=orderfulfillmentstatus...` (sandbox returns mock orders). When a `sold` transition fires, also update the parent `Card`: `status="Sold"`, `sold_price`, `sold_date`, `ebay_status="sold"`, `fee_pct` (defaults to 0.13).
- `async def sync_all_active() -> {checked: int, transitions: int}` — pulls every `Listing` with `status="active"`, runs `sync_listing` with retry+backoff. Throttles to ≤2 RPS so we stay friendly with eBay's rate limits.

### A2. Sales router (`app/routers/sales.py`)

- `POST /api/sales/sync` — fire-and-forget; returns `{queued: True}` and runs `sync_all_active` in a background task. Idempotent; can be called from a UI button or scheduled.
- `GET /api/sales/summary` — returns:
  ```json
  {
    "realized_total": 1847.50,
    "realized_net": 1606.32,
    "fees_total": 241.18,
    "by_month": {"2026-04": {...}, "2026-05": {...}},
    "by_channel": {"eBay": {...}, "FB": {...}},
    "active_value": 432.00,
    "draft_value": 89.00,
    "sell_through_days_avg": 11.2
  }
  ```
- `GET /api/sales/recent?n=20` — most recent sold cards (joined Card + Listing data, ordered by `sold_at` DESC).
- `POST /api/sales/{listing_id}/mark-sold-manual` — replacement for the existing `/api/listings/{id}/mark-sold` for sales that didn't go through eBay (LCS, Facebook). Body `{sold_price, fees?, channel, sold_at?}`. Updates Card + Listing.

### A3. Card / Listing model adds

Card additions (migration `m7_sales_tracking` id=7):

- `sold_at: Optional[datetime]` — when the sale actually occurred (not just when we noticed).
- `sale_channel: Optional[str]` — `"eBay"` / `"FB"` / `"LCS"` / etc. (replaces ad-hoc `channel` — keep `channel` as a free-form preference).
- `acquisition_cost: Optional[float]` — what Connor paid (manual input; future Round 5 candidate is auto-import from box-purchase logs).

Listing additions (same migration):

- `last_synced_at: Optional[datetime]` — when sales_sync last touched this row.
- `sync_error: Optional[str]` — last sync error message if any.

### A4. xlsx + Sheets mirror updates

When a Card transitions to `Sold`, `xlsx_mirror.py` should also append a row to the `Sales Log` tab of Connor's existing `Baseball_Cards.xlsx` (column layout already exists — read `Personal Finances/Baseball_Cards.xlsx` first to confirm the schema). The same row goes to a `Sales` tab on the Google Sheet mirror (create if absent).

### A5. Tests

- `tests/test_sales_sync.py` — mock the eBay HTTP, verify status transitions, sold-price/sold-at population, retry on 5xx, no-op when no listings to check.
- `tests/test_sales_router.py` — `/api/sales/summary` math; `/api/sales/recent` ordering; manual-mark-sold path.
- `tests/test_sales_mirror.py` — when a Card flips to Sold, the xlsx Sales Log gets a row.

### A6. Acceptance

- Existing 242 tests still pass.
- `sync_all_active` against a mocked eBay sandbox transitions a fake `active` listing to `sold`, stamps `sold_at`/`sold_price`/`fee_pct` on the Card, and appends to the Sales Log mirror.
- `/api/sales/summary` returns sensible totals on a seeded DB with 10+ sold cards.

---

## Workstream B — Grading submissions + bulk-lot clustering + R3 loose ends

Owner files: `app/services/grading.py` (new), `app/services/bulk_lots.py` (new), `app/routers/grading.py` (new), `app/routers/bulk_lots.py` (new), `app/routers/inventory.py` (only the reidentify endpoint addition + `comp_confidence` filter), `app/routers/stats.py` (only `last_24h` addition), `tests/test_grading_*.py`, `tests/test_bulk_lots_*.py`, `tests/test_reidentify.py`, `tests/test_stats_last_24h.py`.

### B1. Grading submission packet (`app/services/grading.py`)

Two submission templates supported on day one:

- **PSA** — CSV format matching their bulk-submission template: `Item, Year, Brand, Player, Variant, Card #, Service Level, Notes`.
- **SGC** — similar CSV with their column names.

Functions:

- `build_psa_csv(cards: list[Card], service_level: str = "Value") -> str` — returns CSV bytes ready to upload to PSA's submission tool.
- `build_sgc_csv(cards: list[Card]) -> str` — same for SGC.
- `estimate_grading_economics(card: Card, service: Literal["PSA","SGC"], expected_grade: int) -> dict` — given a raw comp median, project post-grade value:
  - PSA 8 → 1.5×, PSA 9 → 3×, PSA 10 → 8× (rough market multipliers; pull from a const dict, easy to tune)
  - Cost: PSA Value $25, PSA Regular $75, SGC Standard $30, etc.
  - Returns `{cost, projected_value, projected_net, breakeven_grade}` — so the UI can show "Breakeven at PSA 8.5".

### B2. Grading router (`app/routers/grading.py`)

- `POST /api/grading/build-submission` — body `{card_ids: [int], service: "PSA"|"SGC", service_level: str}`. Returns the CSV as a streamed download (`Content-Type: text/csv`, `Content-Disposition: attachment`).
- `POST /api/grading/queue` — body same plus `{service_level}`. Updates each Card: `status="Pending Grading"`, appends a note `"Sent for {service} {service_level} grading on YYYY-MM-DD"`. Adds a new `Card.grading_submission_id` (uuid) so the user can track which batch they're in.
- `GET /api/grading/queues` — returns active grading queues grouped by `grading_submission_id` with card count and estimated cost.
- `POST /api/grading/{submission_id}/mark-back` — body `[{card_id, grade}]`. For each card: set `is_graded=true`, `grade=f"{service} {grade}"`, `status="Researching"` (so Connor can re-comp and re-list at graded prices), recompute comps in background.

### B3. Bulk-lot clustering (`app/services/bulk_lots.py`)

- `cluster_for_lots(cards: list[Card]) -> list[BulkLotProposal]` — pure function. Groups sub-$1 cards (effective value < 1.0, `status != "Bulk"` already) into proposed lots. Clustering keys (in priority order):
  1. `(year, set_brand)` — "1989 Donruss"
  2. `(year_decade, sport)` — "1990s baseball commons"
  3. `(era,)` — fallback
- Each `BulkLotProposal` returns: `{card_ids, cluster_label, count, estimated_value (sum of comps), suggested_title, suggested_price}`.
- Suggested title pattern: `"Lot of {N} {label} commons — {representative_players}"`. Pull 3 highest-value player names as representatives.
- Suggested price: take `sum_of_comps * 0.7` as starting bid (people pay less per card in bulk).
- Cap proposed lots at 100 cards each (eBay buyers don't want a 500-card lot in one box).

### B4. Bulk-lots router (`app/routers/bulk_lots.py`)

- `GET /api/bulk-lots/proposals` — returns the live cluster list. Cached for 5 minutes (recompute lazy on first call after expiry).
- `POST /api/bulk-lots/create` — body `{card_ids, label, listing_title, price}`. Creates a new `BulkLot` row (see B6) linking the cards, sets each card's `status="Bulk"` and `lot_id`. Returns `{bulk_lot_id}`.
- `POST /api/bulk-lots/{lot_id}/list-on-ebay` — drafts an eBay listing for the lot using the existing `ebay_listing.publish_listing` path with a "Lot" category. Uses the proposal's `listing_title` and `price`.

### B5. Wire the missing R3 endpoint

`POST /api/inventory/{id}/reidentify` in `app/routers/inventory.py`:

- Loads the card.
- Looks up its `front_image` / `back_image` files (`UPLOAD_DIR / fname`).
- Calls `claude_vision.identify_card_async` with the API key from header (`X-Anthropic-Key`) or env.
- Merges the result into the Card row (only overwrite fields the user hasn't manually changed — tracked via a new `Card.user_overrides: TEXT` JSON list, defaulted to `[]`).
- Returns the updated `CardOut`.

### B6. Model additions (same migration `m8_grading_bulk` id=8)

- `Card.grading_submission_id: Optional[str]` — uuid string, links cards in the same submission.
- `Card.user_overrides: Optional[str]` — JSON-encoded list of field names the user has manually edited; reidentify respects these.
- `Card.lot_id: Optional[int]` — foreign key to new BulkLot table.
- `Card.acquisition_cost` — also belongs here if A doesn't ship first.

New table `BulkLot`:
```python
class BulkLot(SQLModel, table=True):
    id: Optional[int] = primary_key
    label: str
    listing_title: Optional[str]
    price: Optional[float]
    status: str = "draft"  # draft / listed / sold
    ebay_listing_id: Optional[str]
    sold_price: Optional[float]
    created_at: datetime
```

### B7. Server-side `comp_confidence` filter on `/api/inventory`

Add `comp_confidence: Optional[str]` query param (one of `high`/`medium`/`low`). When set, filter accordingly. Update the frontend `_buildInventoryParams` if needed.

### B8. `GET /api/stats/last_24h`

Add to `app/routers/stats.py` (this is the only file Workstream B touches in `stats.py`):

```json
{
  "scanned": 47,
  "hits": 6,
  "value_added": 312.50,
  "ids": [123, 124, ...]
}
```

Cards counted = `created_at >= utcnow() - 24h`. The activity stripe (R2-B5) consumes this.

### B9. Tests

- `tests/test_grading_economics.py` — PSA 9 / 10 / 8 multipliers, breakeven math, service-level costs.
- `tests/test_grading_csv.py` — PSA + SGC CSV format matches schema; special chars escaped.
- `tests/test_grading_queue.py` — submission queue lifecycle: build → queue → mark-back.
- `tests/test_bulk_lots_clustering.py` — `cluster_for_lots` groups by year/set, fallback to decade, then era. 100-card cap enforced.
- `tests/test_bulk_lots_create.py` — POST /create flips card status to Bulk and assigns `lot_id`.
- `tests/test_reidentify.py` — endpoint calls Claude (mocked), merges result, respects `user_overrides`.
- `tests/test_stats_last_24h.py` — counts only last-24h cards, value sum correct, ids list ordered.

### B10. Acceptance

- All R1+R2+R3 tests still pass.
- `POST /api/grading/build-submission` returns a valid PSA CSV downloadable from the dashboard.
- `GET /api/bulk-lots/proposals` returns sensible groupings on a seeded set with 500+ sub-dollar cards.
- `POST /api/inventory/{id}/reidentify` round-trips against a mocked Claude and updates only un-overridden fields.
- `GET /api/stats/last_24h` returns counts on the seeded DB.

---

## Workstream C — Frontend: Money tab + grading flow + bulk-lot UI

Owner files: `app/static/index.html`, `app/static/app.js`, `app/static/style.css`, `tests/test_static_assets.py`. **No Python files** except the test file.

### C1. New "Money" tab

Add `Money` to the tab nav (between `Listings` and `Achievements`). Body:

- **Top stats card row:** Realized Net (lifetime), This Month, Fees Total, Sell-through (avg days).
- **Monthly P&L chart** (Chart.js bar — gross green, fees red, net solid green line overlay). Pulls from `/api/sales/summary.by_month`.
- **Channel breakdown** (donut chart): eBay / FB / LCS / Other proportions of realized.
- **Recent sales table** (last 20, with player, sold price, fee, net, channel, sold date).
- **Manual sale entry button** (modal): POST `/api/sales/{listing_id}/mark-sold-manual` for cards that sold off-eBay.
- "Sync eBay sales" button at top right — hits `POST /api/sales/sync`, shows a small spinner until done.

### C2. Grading flow

Replace the R3 Grade info modal in the Suggestions tab. New flow:

1. Click "Grade" on any suggestion → check-box added to a "Grading queue" sidebar.
2. Sidebar shows running totals (count, estimated cost, projected post-grade value).
3. "Send batch" button → modal asks `service` (PSA or SGC), `service_level` (Value / Regular / Express), then calls `POST /api/grading/build-submission` to download the CSV. After the CSV downloads, calls `POST /api/grading/queue` to mark the cards.
4. Card rows show a small "📦 PSA" or "📦 SGC" chip when `grading_submission_id` is set.
5. New "Grading queues" panel on the Money tab listing in-flight submissions with cards. "Mark batch back" button opens a modal where the user enters grades per card.

### C3. Bulk-lot proposer

New "Bulk Lots" sub-tab under Inventory (or a sidebar trigger — your call). Calls `GET /api/bulk-lots/proposals`. Each proposal card shows:

- Label ("1989 Donruss baseball commons"), count, est value, suggested title, suggested price.
- "Build lot" button → opens a modal listing all card IDs (with checkboxes to remove any). Title and price are editable. Submit → POST `/api/bulk-lots/create`.
- "Build + list on eBay" button → does the above plus immediately calls `POST /api/bulk-lots/{id}/list-on-ebay`.

### C4. Re-identify button (R3 completion)

The R3 frontend has the button but the endpoint didn't exist. Now it does (B5). Wire it up — on click, show a small spinner inside the modal, call the endpoint, replace `editing` with the response, toast on success/error.

### C5. Stats stripe upgrade

Replace the client-side last-24h derivation with `GET /api/stats/last_24h`. Stripe text comes straight from the response now. (The fallback derivation can stay as a backup if the endpoint fails.)

### C6. Tests (extend `tests/test_static_assets.py`)

- `Money` string in nav array.
- `loadSalesSummary` or equivalent fetch fn in `app.js`.
- `/api/sales/`, `/api/grading/`, `/api/bulk-lots/`, `/api/stats/last_24h`, `/reidentify` references present.
- `Grading queue` and `Bulk Lots` heading strings.

---

## Workstream D — Coordination, docs, commit (orchestrator)

After A/B/C deliver:

- Run full test suite — should pass.
- Verify on a 1k seeded DB: `/api/sales/summary`, `/api/grading/build-submission` (manual download check), `/api/bulk-lots/proposals`.
- Append `## Round 4 — Closing the loop` section to `IMPROVEMENTS.md`.
- Add a "Selling and grading" section to `README.md`.
- **Delete the orphan `.claude/worktrees/beautiful-hugle-202eb5/`** worktree now that R3 has fully superseded it.

---

## Coordination notes

- **No file collisions:** A and B both add migrations (A=7, B=8). A doesn't touch grading/bulk-lot files; B doesn't touch sales/listings files beyond the small reidentify+stats additions explicitly listed. C is static-only.
- **`Card.acquisition_cost`** lives in A's migration. If B ships first, it can no-op on that field; we'll add it later.
- **Mocking:** every test must mock eBay HTTP. Use `unittest.mock.patch.object(ebay_listing, "_access_token", new=AsyncMock(return_value="t"))` plus `patch("httpx.AsyncClient.get", ...)`/`patch("httpx.AsyncClient.put", ...)`.
- **Git:** working tree has R1+R2+R3 changes uncommitted (lockfile situation, see IMPROVEMENTS.md). Don't `git reset` or modify history.
- **Don't touch `.claude/worktrees/`** until Workstream D deletes it.

---

## Success criteria

1. All R1+R2+R3+R4 tests pass (current 242 → target ≥320).
2. `/api/sales/summary` returns realistic P&L on a seeded DB with mock sold cards.
3. `/api/grading/build-submission` downloads a valid PSA CSV.
4. `/api/bulk-lots/proposals` returns sensible groupings.
5. `POST /api/inventory/{id}/reidentify` actually works (no longer a frontend TODO).
6. Money tab visible, populated, charted.
7. `IMPROVEMENTS.md` has Round 4 section.
