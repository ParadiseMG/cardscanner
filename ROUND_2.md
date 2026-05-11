# Round 2 — Performance & UX at scale

Round 1 made the system *not break*. Round 2 makes it *not crawl*.

**Premise:** the MVP renders fine with 5 cards. With 3,000 (Connor's actual collection size), it becomes unusable in a hurry — the front-end re-renders the whole inventory on every change, search and sort are client-side, no bulk operations beyond "list selected", and a server restart strands any in-flight ScanJob. This round fixes that.

**Out of scope for Round 2:** new identification fields, suggestion engine, smarter comp analysis. Those land in Round 3.

---

## Workstream A — Backend: server-side query, bulk ops, batch resume

Owner files: `app/routers/inventory.py`, `app/routers/bulk.py` (new), `app/routers/scan.py`, `app/pipeline.py`, `app/migrations.py`, `app/models.py` (additive only — no breaking field changes).

### A1. Extend `GET /api/inventory` with proper server-side query

Add query params:

- `sort` — one of `recent` (default), `value_desc`, `value_asc`, `year_desc`, `year_asc`, `player_az`.
- `era` — exact-string match against the `Card.era()` bucket (e.g. `"Junk Wax (1986-1991)"`). Multi-valued via repeated query param.
- `ebay_status` — exact match: `not_listed`, `drafted`, `active`, `sold`.
- `min_value` / `max_value` — float bounds against `COALESCE(comp_median, est_value_raw, 0)`.
- `autograph`, `relic`, `graded` — booleans.
- All filters compose with `q`, `hit_only`, `needs_review` from Round 1.
- Response unchanged shape (`{total, items}`) but `total` reflects post-filter count, not the page.

### A2. Add `/api/inventory/facets`

Returns aggregate counts that power the filter sidebar so the UI doesn't have to scan all cards itself:

```json
{
  "eras": {"Junk Wax (1986-1991)": 412, ...},
  "ebay_status": {"not_listed": 2700, "drafted": 18, "active": 6, "sold": 2},
  "value_buckets": {"0-1": 1800, "1-10": 900, "10-50": 250, "50-200": 35, "200+": 15},
  "tags": {"autograph": 22, "relic": 14, "graded": 3, "hit": 88}
}
```

Computed in a single pass over `Card`. Should be < 50ms with 3,000 rows on a warm SQLite.

### A3. Bulk operations

New router `app/routers/bulk.py`:

- `POST /api/bulk/ids` — accept a filter spec (same params as `/api/inventory`) and return *just the matching card IDs* (`{ids: [...], total: N}`). Lets the UI "select all matching filter" without paging.
- `POST /api/bulk/patch` — body `{ids: [int], patch: {status?, channel?, notes_append?}}`. Applies the patch to every listed card in a single transaction. Returns `{updated: N}`.
- `POST /api/bulk/delete` — body `{ids: [int]}`. Soft-delete (set `Card.status = "Deleted"`) so we can recover from accidents. Inventory queries should exclude `status == "Deleted"` by default.
- `POST /api/bulk/recompute-comps` — body `{ids: [int]}`. Queue a re-comp run for each card (don't block the request). Reuses the existing `pipeline.fetch_comps`. Returns `{queued: N}`.
- `POST /api/bulk/move-to-bulk-lot` — body `{ids: [int], lot_label: str}`. Sets `status = "Bulk"`, appends a `lot_label` note, suitable for the Bulk Lots tab in the xlsx mirror.

### A4. Batch resume

When `init_db()` runs at startup, scan for `ScanJob` rows with `status in ("queued", "processing")` and `finished_at is NULL`. For each:

- If `started_at` is older than 60 minutes → mark as `status = "abandoned"` and stop.
- Otherwise → kick off an asyncio task that loads the *un-processed* portion of the job. To know what's un-processed, store the list of source files on the job. Add a column `source_manifest: TEXT (JSON)` to `ScanJob` listing the image paths/IDs the job was supposed to handle. Diff against what's already saved to recover the remainder.

Migration `m3_add_scanjob_manifest` adds the column. Don't break Round 1 — the column is nullable; old jobs without a manifest just stay marked done.

### A5. Indexes (migration `m3_perf_indexes`)

Run as a single migration (id=3):

```sql
CREATE INDEX IF NOT EXISTS ix_card_status ON card(status);
CREATE INDEX IF NOT EXISTS ix_card_ebay_status ON card(ebay_status);
CREATE INDEX IF NOT EXISTS ix_card_comp_median ON card(comp_median DESC);
CREATE INDEX IF NOT EXISTS ix_card_year ON card(year);
CREATE INDEX IF NOT EXISTS ix_card_is_hit ON card(is_hit_watchlist);
CREATE INDEX IF NOT EXISTS ix_card_review ON card(review_flagged);
CREATE INDEX IF NOT EXISTS ix_scanjob_status ON scanjob(status);
```

### A6. Tests (Workstream A)

- `test_inventory_query.py` — every filter and sort produces the expected ordering / count. Multi-filter combination works.
- `test_bulk_ops.py` — patch, delete (soft), recompute-comp queue, move-to-bulk-lot. Idempotent on repeat.
- `test_batch_resume.py` — fake a stranded job with a manifest, restart, verify only the unprocessed half re-runs. Verify > 60min jobs are abandoned, not retried.
- `test_facets.py` — counts roll up correctly across a synthesized set of cards.

---

## Workstream B — Frontend: virtualization, keyboard, real-time stripe

Owner files: `app/static/index.html`, `app/static/app.js`, `app/static/style.css`. May add libraries from a CDN. Do **not** touch Python files.

### B1. Inventory virtualization

Replace the current `<template x-for>` over the whole `inventory` array with a virtual scroller. Use `alpinejs-intersect` or a plain windowed-list pattern (compute `viewportTop / rowHeight` → render only rows in that range). The dashboard should stay smooth with 5,000 rows.

Acceptance: scrolling a 3,000-card inventory keeps frame time under 16ms on a mid-range Mac. (Verify via DevTools Performance tab; document the result in IMPROVEMENTS.)

### B2. Server-driven sort + filter + search

Stop client-filtering. Every filter / sort / search change re-issues `/api/inventory?...` with the new params (debounced 300ms for `q`). The new facet counts come from `/api/inventory/facets` and render as a sidebar with chip-style toggles.

### B3. Bulk-action affordances

- "Select all matching filter (N)" button — hits `POST /api/bulk/ids`, lights up the bulk-actions toolbar with the returned ID list.
- Toolbar appears when ≥1 card selected. Actions: Status →, Channel →, Add note, Recompute comps, Move to bulk lot, Delete. Each calls the corresponding `/api/bulk/...` endpoint, then refreshes inventory + facets.
- Confirm dialog before delete and move-to-bulk-lot (these are destructive).

### B4. Keyboard navigation

Within the inventory list:

- `j` / `k` — move row focus down / up.
- `x` — toggle select on focused row.
- `e` — open edit modal for focused row.
- `l` — open eBay listing preview for focused row.
- `/` — focus search box.
- `g h` — jump to top.
- `?` — open keyboard shortcuts help overlay.
- `Escape` — close modal / clear focus.

Show a subtle hint on the inventory table's first visit: "Press ? for shortcuts".

### B5. Persistent activity stripe

A thin band above the inventory table that always shows the last-24h stats: `"412 scanned · 39 hits · $1,847 added"`. Tap the chip to filter the inventory to "added today". Stays visible whether or not a job is currently running.

While a job *is* running, show the progress bar in the same stripe (replacing or layering over the stats summary). When the job finishes, fade the progress bar out and surface a "View N new cards" link.

### B6. Frontend perf polish

- Lazy-load card thumbnail images (`loading="lazy"`).
- Memoize chart data — only re-render Era/Player charts when stats payload changes (use a hash compare on the `era_distribution + top_players` JSON).
- Use `requestIdleCallback` for non-critical work like refreshing facets after a write.

### B7. Tests (Workstream B)

- Lightweight DOM smoke test using `playwright` is overkill for this round. Instead, add a small `test_static_assets.py` that pulls `/static/app.js` and asserts the new functions (`virtualizeInventory`, `applyFilters`, `keyboardHandler`) are defined.
- Manual verification: load 3,000 fake cards via a `scripts/seed_fake.py` helper, scroll the inventory, run bulk-select, confirm it stays responsive.

---

## Workstream C — Shared / coordination

These are owned by *neither* workstream — handle at the end:

- Update `README.md`'s "Daily workflow" section with the new keyboard shortcuts and bulk actions.
- Append a `## Round 2 — Performance & UX at scale` section to `IMPROVEMENTS.md` listing every change.
- Run the full test suite (`pytest tests/ -q`) and confirm it passes before committing.
- `scripts/seed_fake.py` (new) — populates the DB with N synthetic cards for local perf testing. Used in manual verification above.

---

## Coordination notes for the agents

- **No collisions:** Workstream A only touches Python under `app/` and `tests/`. Workstream B only touches files under `app/static/`. The owner-file lists above are exhaustive.
- **Contract is fixed before the work starts.** Both agents may treat the API shapes in A1/A2/A3 as final — if they need to evolve, raise it via the IMPROVEMENTS.md, don't silently change.
- **Round 1 changes are in the working tree, uncommitted.** Do not run `git reset` or modify history. The current `main` branch HEAD is the MVP commit; treat the working tree as the baseline.
- **Don't touch `.claude/worktrees/`** — that's the orphan Round 3 exploration, will be reconciled separately.

---

## Success criteria for Round 2 close-out

1. Inventory remains interactive (under 200ms perceived latency on any action) with 3,000 seeded cards.
2. All Round 1 tests + new Round 2 tests pass.
3. Bulk-select + bulk-patch works end-to-end via the dashboard.
4. Keyboard shortcuts all functional.
5. Restarting the server mid-batch resumes the unprocessed remainder.
6. `IMPROVEMENTS.md` has the Round 2 change list.
