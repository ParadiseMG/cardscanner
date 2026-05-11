# Round 3 — Intelligence

Round 1 made the system *not break*. Round 2 made it *not crawl*. Round 3 makes it *think*.

**Premise:** the dashboard already does identification + comps + listings. But every card gets the same treatment regardless of how confident the AI is, how well the comps converge, or how the card relates to the rest of the collection. Round 3 surfaces signal: "this card's vision call was shaky", "these comps look like a bulk-lot fingerprint", "you should grade this one before listing", "you have 47 sub-dollar 1989 Donruss commons — bundle them".

**Themes:**
1. **Smarter vision** — per-field confidence, structured condition signals, low-confidence re-prompt, rookie/serial-number detection.
2. **Smarter comps** — recency weighting, bulk-lot fingerprint detection, comp confidence rating.
3. **Proactive suggestions** — surface what to do next instead of waiting for the user to scroll the inventory looking for it.

**Out of scope for Round 3:** any further UI virtualization, payment/sales tracking, multi-sport support. Those are Round 4 territory.

**Reference material:** there's an orphan worktree at `.claude/worktrees/beautiful-hugle-202eb5/` that started a parallel exploration of these themes. Tests in that worktree (`test_suggestions.py`, `test_vision_parser.py`, `test_comp_filtering.py`) are good thought-starters and the contract IDs in this spec match what they expected. **Do not** branch from that worktree — start from current `main` working tree (which already has Round 1 + Round 2 changes uncommitted in it).

---

## Workstream A — Smarter vision

Owner files: `app/services/claude_vision.py`, `app/models.py` (additive only — see field list), `app/pipeline.py` (only the lines that propagate vision output to the Card row), `tests/test_vision_*.py`.

### A1. Enriched `CardIdentification` dataclass

Add these fields (all default to `None`/`False`/`{}` so existing code keeps working):

```python
@dataclass
class CardIdentification:
    # ... existing fields unchanged ...
    is_rookie: bool = False
    is_serial_numbered: bool = False
    serial_print_run: Optional[int] = None       # e.g. 50 from "/50"
    field_confidence: dict[str, float] = field(default_factory=dict)
    condition_signals: dict[str, str] = field(default_factory=dict)
    low_confidence_fields: list[str] = field(default_factory=list)
    photo_quality: Optional[str] = None          # "good" / "blurry" / "obstructed" / "off_angle"
```

The Claude prompt in `claude_vision.py` should ask for these explicitly. Update `_PROMPT` to extend the JSON schema (still strict-JSON output, single message). `field_confidence` should cover at least `year`, `set_brand`, `player`, `card_no`, `parallel`, `condition` — float in `[0.0, 1.0]`. `condition_signals` should include any of `centering` (e.g. "55/45"), `corners`, `edges`, `surface` that Claude can infer from the photo.

### A2. Low-confidence re-prompt

When `field_confidence[field] < 0.5` for any of `parallel` or `card_no` (the two most often wrong on first pass), automatically issue a focused follow-up call to Claude — pass the same image plus a tighter prompt asking *only* about that field. Merge the answer back into the identification, bumping that field's confidence accordingly.

Cap the re-prompt at 1 retry per card and 2 fields total. Log each re-prompt via the structured logger (`step("vision_reprompt", field=...)`).

If after re-prompting the `low_confidence_fields` list is still non-empty, set `Card.review_flagged = True` and stamp `Card.notes` with `"Auto-flagged: low confidence on {fields}"`.

### A3. Card model fields (additive — non-overlapping with Workstream B)

Add to `Card`:

```python
is_rookie: bool = False
is_serial_numbered: bool = False
serial_print_run: Optional[int] = None
photo_quality: Optional[str] = None
low_confidence_fields: Optional[str] = None  # JSON-encoded list
condition_signals: Optional[str] = None      # JSON-encoded dict
```

Migration `m5_vision_fields` (id=5) adds these via `ALTER TABLE card ADD COLUMN` (use the same try/except pattern as `m4`).

### A4. Pipeline integration

In `pipeline._process_one`, after the vision call (and after any re-prompt), populate the new `Card` fields from `CardIdentification`:

```python
card.is_rookie = ident.is_rookie
card.is_serial_numbered = ident.is_serial_numbered
card.serial_print_run = ident.serial_print_run
card.photo_quality = ident.photo_quality
card.low_confidence_fields = json.dumps(ident.low_confidence_fields) if ident.low_confidence_fields else None
card.condition_signals = json.dumps(ident.condition_signals) if ident.condition_signals else None
```

Don't touch the comp-handling lines — those are Workstream B.

### A5. Tests

- `tests/test_vision_parser.py` — JSON parser handles the enriched response shape; missing fields default safely; markdown fence stripping still works.
- `tests/test_vision_reprompt.py` — when a field's confidence is below 0.5, a re-prompt fires; cap at 1 retry per card; merged result has bumped confidence.
- `tests/test_pipeline_vision_fields.py` — after `_process_one`, the Card row has `is_rookie`, `condition_signals` (as JSON string), etc. populated.
- Mock the Anthropic HTTP call — no live network. Use `unittest.mock.patch.object(claude_vision, "identify_card_async", ...)` and assert call counts for the re-prompt logic.

### A6. Acceptance

- Existing 132 tests still pass.
- Re-prompt path verified end-to-end against a mocked Claude that returns low confidence on `parallel` first call, high confidence second call.
- New Card fields visible via `GET /api/inventory/{id}`.

---

## Workstream B — Smarter comps + suggestion engine

Owner files: `app/services/comp_lookup.py`, `app/models.py` (additive only — see field list, non-overlapping with A), `app/pipeline.py` (only the lines that propagate comp output to the Card), `app/routers/suggestions.py` (new), `app/routers/stats.py` (only the `action_queue` endpoint — see C-section note), `tests/test_comp_*.py`, `tests/test_suggestions.py`.

### B1. Smarter comp analysis (`comp_lookup.py`)

Three new pure-function helpers (all unit-testable without network):

- `filter_outliers(prices: list[float]) -> list[float]` — for samples ≥10 prices, trim 10% from each end; otherwise return unchanged. Already partially exists; lift to a public name and cover with explicit tests.
- `detect_suspicious(prices: list[float]) -> tuple[bool, str]` — returns `(suspicious, reason)`. Suspicious if ≥5 of the prices are within 1% of each other ("classic bulk-lot fingerprint at $X"). Diverse pricing returns `(False, "")`.
- `recency_weighted(samples: list[tuple[float, datetime|None]]) -> Optional[float]` — weighted median where samples in the last 7 days get weight 3, last 30 days weight 2, older weight 1. If any sample lacks a date, fall back to plain median. Empty list returns `None`.

Wire these into `fetch_comps`:

- After scraping, parse the sale dates if available (eBay sold pages list dates). Store dates alongside prices in `CompResult.samples: list[tuple[float, Optional[datetime]]]`.
- Compute `CompResult.suspicious_bulk: bool` and `CompResult.suspicious_reason: str`.
- Compute `CompResult.median_recency_weighted: Optional[float]` alongside the plain median.
- Add `CompResult.confidence: str` — `"high"` (≥10 samples, no suspicion, low spread), `"medium"` (5-9 samples), `"low"` (<5 samples or suspicious or huge spread). Define spread: `(high - low) / median > 2.0`.

Expose test hooks: `filter_outliers_for_test`, `detect_suspicious_for_test`, `recency_weighted_for_test`.

### B2. Card model fields (additive — non-overlapping with A)

Add to `Card`:

```python
comp_confidence: Optional[str] = None         # high/medium/low/None
comp_median_weighted: Optional[float] = None
comp_suspicious_bulk: bool = False
comp_suspicious_reason: Optional[str] = None
```

Migration `m6_comp_intelligence` (id=6) adds these.

### B3. Pipeline integration

In `pipeline._process_one`, after `comp_lookup.fetch_comps`, populate:

```python
card.comp_confidence = comp.confidence
card.comp_median_weighted = comp.median_recency_weighted
card.comp_suspicious_bulk = comp.suspicious_bulk
card.comp_suspicious_reason = comp.suspicious_reason
```

When `comp.suspicious_bulk` is True, also append a note to `card.notes`: `"⚠️ Comp prices look like a bulk-lot fingerprint — verify before pricing"`.

Use `comp_median_weighted or comp_median` everywhere downstream code currently uses `comp_median` for "what's this card worth?" — start with `app/services/xlsx_mirror.py` and `app/services/sheets_sync.py`. (Verify by grep.)

### B4. Suggestion engine — `app/routers/suggestions.py` (new)

`GET /api/suggestions` returns `{items: [{id, kind, title, value, reason}]}` where `kind` is one of:

- `"grade"` — raw card (`is_graded == False`) with `comp_median_weighted or comp_median ≥ 80`. Reason: "Grading cost ~$25 PSA — projected post-grade value $X (assumes 9)".
- `"bulk"` — card with effective value `< 1.0` and `status not in {"Bulk", "Sold", "Deleted"}`. Reason: "Sub-dollar single — bundle into a bulk lot".
- `"list"` — card with `is_hit_watchlist == True` and `ebay_status == "not_listed"` and `status != "Sold"`. Reason: "Hit Watchlist match — list it before it cools off".
- `"reshoot"` — card with `photo_quality in {"blurry", "obstructed", "off_angle"}`. Reason: "Photo quality flagged — better photo helps both ID and listing".
- `"verify_comps"` — card with `comp_suspicious_bulk == True`. Reason from `comp_suspicious_reason`.

Each item also returns `value` (the card's effective value) so the UI can sort by impact.

Cap response at 100 items, sorted by `value DESC`.

### B5. Action queue extension

Modify `app/routers/stats.py::action_queue` to add a `"low_comp_confidence"` bucket: cards with `comp_confidence == "low"` AND value ≥ $5 (low-value cards aren't worth manually re-checking). Keep existing buckets unchanged.

### B6. Tests

- `tests/test_comp_filtering.py` — `filter_outliers`, `detect_suspicious`, `recency_weighted` per spec contracts.
- `tests/test_suggestions.py` — each of the five suggestion kinds; cap at 100; ordering by value desc; cards in wrong status are excluded.
- `tests/test_action_queue_low_comp.py` — new bucket present, value floor respected.
- `tests/test_pipeline_comp_fields.py` — after `_process_one`, the new Card comp fields are populated; suspicious case triggers note append.

### B7. Acceptance

- Existing 132 tests still pass.
- `GET /api/suggestions` works on the seeded 3k-card DB and returns sensible items in <100ms.
- Suspicious-bulk detection fires on the test fixture (5+ prices at the same value) and not on diverse pricing.

---

## Workstream C — Frontend: suggestions panel + intelligence surfacing

Owner files: `app/static/index.html`, `app/static/app.js`, `app/static/style.css`. May also touch `tests/test_static_assets.py`. **No Python files.**

### C1. New "Suggestions" tab

Add `Suggestions` to the tab nav (between `Inventory` and `Listings`). Fetches `GET /api/suggestions`, renders one card per item grouped by `kind` (Grade / Bulk / List / Reshoot / Verify Comps). Each card shows:

- The card's title + thumbnail (use existing image URL pattern).
- The suggestion reason.
- Value chip (so you can scan high-impact items first).
- A primary action button per kind:
  - **Grade**: opens an info modal (no API yet — Round 4 will integrate PSA/SGC submission). Mark item as "dismissed" via local state for now.
  - **Bulk**: opens the existing bulk-action toolbar pre-populated with these IDs and `Move to bulk lot` selected.
  - **List**: opens the eBay listing preview modal for that card.
  - **Reshoot**: opens edit modal with `needs_photo_verification` checkbox checked.
  - **Verify Comps**: opens the card's edit modal scrolled to the notes section.

Counts in the tab label: `"Suggestions (47)"`. Refresh on tab focus.

### C2. Intelligence chips on every card row

In the inventory table, augment the existing tag chips (HIT / auto / relic / graded) with:

- **🟢/🟡/🔴 confidence dot** — `comp_confidence` value (`high`/`medium`/`low`). Tooltip on hover: "Comp confidence: high (12 samples, low spread)".
- **RC** — when `is_rookie`.
- **/N** chip — when `is_serial_numbered` (e.g. `/50`).
- **⚠** badge — when `comp_suspicious_bulk` or `low_confidence_fields` is non-empty. Tooltip: the reason.

Keep chip density readable — fold less-critical chips into a "+2 more" overflow on narrow screens.

### C3. Edit modal: surface intelligence fields

When opening a card's edit modal:

- Show `condition_signals` (parsed from JSON) as a small read-only block: "Centering 55/45 · Corners sharp · Edges rough".
- Show per-field confidence next to the corresponding inputs (small grey number, e.g. "Parallel: 0.42").
- Add a "Re-identify with Claude" button — calls `POST /api/inventory/{id}/reidentify` (Workstream B owns this — if it's not present yet, the button is hidden and the spec is fine). For now, just lay out the UI; if the endpoint exists, wire it; if not, leave a TODO comment.

### C4. Action queue bumps `low_comp_confidence`

The Action Queue panel on the Dashboard tab already shows `needs_review`, `consider_grading`, `needs_photo_verification`. Add a fourth row: `low_comp_confidence` (count from `/api/action-queue` response).

### C5. Tests

Extend `tests/test_static_assets.py` to assert the served `app.js` and `index.html` reference:

- A `loadSuggestions` function and `/api/suggestions`.
- A `Suggestions` tab name in the nav.
- A `low_comp_confidence` reference in the action queue display.
- Helper functions for the new chips: `confidenceDotColor(conf)` or similar.

### C6. Acceptance

- Suggestions tab appears, loads, groups items by kind.
- Inventory rows render the new chips without breaking virtualization (chips must fit within `ROW_HEIGHT=48px`).
- Edit modal shows the new intelligence fields.

---

## Workstream D — Coordination, docs, commit (handled by orchestrator)

These are owned by the orchestrator after A/B/C deliver:

- Run full test suite (`pytest tests/ -q`) and confirm passing.
- Append `## Round 3 — Intelligence` section to `IMPROVEMENTS.md` with the change list.
- Add a "What the AI is doing for you now" section to `README.md` highlighting the three suggestion kinds and the confidence dot.
- Update `IMPROVEMENTS.md` git note to cover R1 + R2 + R3.

---

## Coordination notes

- **No file collisions:** A and B both touch `pipeline.py` and `models.py` — but in disjoint sections clearly marked in this spec (A: vision-output lines + vision fields; B: comp-output lines + comp fields). Do not edit each other's lines.
- **Migration IDs:** A=5, B=6. Do not collide.
- **Card model:** both agents add fields. They are listed exhaustively per workstream above. Verify before opening a PR-equivalent edit that the union of A's fields + B's fields exists on the model with no duplicates.
- **Git:** working tree is uncommitted (R1 + R2 changes pending — git lockfile situation documented in IMPROVEMENTS.md). Don't `git reset` or modify history; treat the working tree as the baseline.
- **Don't touch `.claude/worktrees/`** — the orphan exploration. It will be deleted in Workstream D after R3 ships.

---

## Success criteria for Round 3 close-out

1. All R1 + R2 + R3 tests pass (current 132 → target ≥160).
2. `/api/suggestions` returns sensible grade/bulk/list/reshoot/verify-comps items on the 3k-seeded DB.
3. Vision re-prompt fires once for low-confidence parallel/card_no fields.
4. Inventory rows show the 🟢/🟡/🔴 confidence dot and any earned RC/serial chips.
5. Suggestions tab is the new front-and-center "what should I do next?" surface.
6. `IMPROVEMENTS.md` has the Round 3 section.
