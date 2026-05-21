"""A3: Bulk operations — IDs, patch, delete, recompute-comps, move-to-bulk-lot."""
from __future__ import annotations

import asyncio
from typing import Optional, List, Any

from fastapi import APIRouter
from pydantic import BaseModel
from sqlmodel import Session, select

from app import models
from app.db import get_engine, session_scope
from app.utils import logger as _log

router = APIRouter()

# Hold references to fire-and-forget background tasks so they don't get GC'd
# mid-execution — asyncio.create_task only stores a weak reference, and the
# bulk recompute loop took 0/32 because the task was collected before its
# first await resolved.
_BG_TASKS: "set[asyncio.Task]" = set()
log = _log.get(__name__)


# ---------------------------------------------------------------------------
# Shared filter spec (mirrors A1 query params as a POST body)
# ---------------------------------------------------------------------------

class FilterSpec(BaseModel):
    """Same filter surface as GET /inventory but as a JSON body."""
    status: Optional[str] = None
    hit_only: bool = False
    needs_review: bool = False
    q: Optional[str] = None
    sort: str = "recent"
    era: List[str] = []
    ebay_status: Optional[str] = None
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    autograph: Optional[bool] = None
    relic: Optional[bool] = None
    graded: Optional[bool] = None
    include_deleted: bool = False


def _card_value(c: models.Card) -> float:
    if c.comp_median is not None:
        return c.comp_median
    if c.est_value_raw is not None:
        return c.est_value_raw
    return 0.0


def _apply_filter(rows: list[models.Card], spec: FilterSpec) -> list[models.Card]:
    """Apply Python-side filters (era, q, value) that can't be pushed to SQL."""
    if spec.q:
        ql = spec.q.lower()
        rows = [c for c in rows
                if (c.player or "").lower().find(ql) >= 0
                or (c.set_brand or "").lower().find(ql) >= 0]
    if spec.era:
        era_set = set(spec.era)
        rows = [c for c in rows if c.era() in era_set]
    if spec.min_value is not None:
        rows = [c for c in rows if _card_value(c) >= spec.min_value]
    if spec.max_value is not None:
        rows = [c for c in rows if _card_value(c) <= spec.max_value]
    return rows


def _query_cards(s: Session, spec: FilterSpec) -> list[models.Card]:
    """Build and execute the base SQL query, then apply Python-side filters."""
    stmt = select(models.Card)
    if not spec.include_deleted:
        stmt = stmt.where(models.Card.status != "Deleted")
    if spec.status:
        stmt = stmt.where(models.Card.status == spec.status)
    if spec.hit_only:
        stmt = stmt.where(models.Card.is_hit_watchlist == True)
    if spec.needs_review:
        stmt = stmt.where(models.Card.review_flagged == True)
    if spec.autograph is not None:
        stmt = stmt.where(models.Card.is_autograph == spec.autograph)
    if spec.relic is not None:
        stmt = stmt.where(models.Card.is_relic == spec.relic)
    if spec.graded is not None:
        stmt = stmt.where(models.Card.is_graded == spec.graded)
    if spec.ebay_status is not None:
        stmt = stmt.where(models.Card.ebay_status == spec.ebay_status)
    rows = s.exec(stmt).all()
    return _apply_filter(rows, spec)


# ---------------------------------------------------------------------------
# POST /api/bulk/ids
# ---------------------------------------------------------------------------

class BulkIdsResponse(BaseModel):
    ids: List[int]
    total: int


@router.post("/bulk/ids", response_model=BulkIdsResponse)
def bulk_ids(spec: FilterSpec) -> BulkIdsResponse:
    """Return IDs matching the filter spec — lets UI 'select all matching'."""
    with Session(get_engine()) as s:
        with _log.step(log, "bulk_ids", spec=spec.model_dump(exclude_defaults=True)):
            rows = _query_cards(s, spec)
    ids = [c.id for c in rows]
    return BulkIdsResponse(ids=ids, total=len(ids))


# ---------------------------------------------------------------------------
# POST /api/bulk/patch
# ---------------------------------------------------------------------------

class BulkPatchBody(BaseModel):
    ids: List[int]
    patch: dict  # keys: status, channel, notes_append


class BulkPatchResponse(BaseModel):
    updated: int


@router.post("/bulk/patch", response_model=BulkPatchResponse)
def bulk_patch(body: BulkPatchBody) -> BulkPatchResponse:
    """Apply a patch to every listed card in a single transaction.

    Accepted patch keys: status, channel, notes_append, storage_location_id
    (pass null to clear), storage_position.
    """
    from datetime import datetime
    from fastapi import HTTPException
    updated = 0
    # If the patch references a storage_location_id, verify it exists once up
    # front rather than re-fetching on every iteration.
    loc_id = body.patch.get("storage_location_id", "_unset")
    if loc_id != "_unset" and loc_id is not None:
        with Session(get_engine()) as s:
            if not s.get(models.StorageLocation, loc_id):
                raise HTTPException(400, "storage_location_id does not exist")
    with _log.step(log, "bulk_patch", count=len(body.ids), patch=body.patch):
        with session_scope() as s:
            for cid in body.ids:
                c = s.get(models.Card, cid)
                if c is None:
                    continue
                if "status" in body.patch and body.patch["status"] is not None:
                    c.status = body.patch["status"]
                if "channel" in body.patch and body.patch["channel"] is not None:
                    c.channel = body.patch["channel"]
                if "notes_append" in body.patch and body.patch["notes_append"]:
                    existing = c.notes or ""
                    c.notes = (existing + "\n" + body.patch["notes_append"]).strip()
                if loc_id != "_unset":
                    c.storage_location_id = loc_id  # may be None to clear
                if "storage_position" in body.patch:
                    c.storage_position = body.patch["storage_position"]
                c.updated_at = datetime.utcnow()
                s.add(c)
                updated += 1
    return BulkPatchResponse(updated=updated)


# ---------------------------------------------------------------------------
# POST /api/bulk/delete  (soft-delete)
# ---------------------------------------------------------------------------

class BulkIdsBody(BaseModel):
    ids: List[int]


class BulkDeleteResponse(BaseModel):
    deleted: int


@router.post("/bulk/delete", response_model=BulkDeleteResponse)
def bulk_delete(body: BulkIdsBody) -> BulkDeleteResponse:
    """Soft-delete cards (set status = 'Deleted')."""
    deleted = 0
    with _log.step(log, "bulk_delete", count=len(body.ids)):
        with session_scope() as s:
            for cid in body.ids:
                c = s.get(models.Card, cid)
                if c is None:
                    continue
                c.status = "Deleted"
                from datetime import datetime
                c.updated_at = datetime.utcnow()
                s.add(c)
                deleted += 1
    return BulkDeleteResponse(deleted=deleted)


# ---------------------------------------------------------------------------
# POST /api/bulk/recompute-comps
# ---------------------------------------------------------------------------

class BulkRecomputeResponse(BaseModel):
    queued: int


@router.post("/bulk/recompute-comps", response_model=BulkRecomputeResponse)
async def bulk_recompute_comps(body: BulkIdsBody) -> BulkRecomputeResponse:
    """Queue a re-comp run for each card. Does not block the request."""
    with _log.step(log, "bulk_recompute_comps", count=len(body.ids)):
        # Verify cards exist and gather their data while session is open
        card_specs = []
        with Session(get_engine()) as s:
            for cid in body.ids:
                c = s.get(models.Card, cid)
                if c is not None:
                    card_specs.append({
                        "id": c.id,
                        "year": c.year,
                        "set_brand": c.set_brand,
                        "player": c.player,
                        "card_no": c.card_no,
                        "parallel": c.parallel,
                    })

        async def _run_recompute():
            from app.services import comp_lookup
            log.info("bulk_recompute starting", extra={"count": len(card_specs)})
            done = 0
            for spec in card_specs:
                try:
                    comp = await comp_lookup.fetch_comps(
                        spec["year"], spec["set_brand"],
                        spec["player"], spec["card_no"], spec["parallel"],
                    )
                    with session_scope() as s:
                        c = s.get(models.Card, spec["id"])
                        if c:
                            from datetime import datetime
                            c.comp_median = comp.median
                            c.comp_low = comp.low
                            c.comp_high = comp.high
                            c.comp_count = comp.count
                            c.comp_url = comp.url
                            c.comp_fetched_at = datetime.utcnow()
                            s.add(c)
                    done += 1
                    if done % 5 == 0:
                        log.info("bulk_recompute progress",
                                 extra={"done": done, "total": len(card_specs)})
                except Exception as e:
                    log.warning("recompute comp fail",
                                extra={"card_id": spec["id"], "error": str(e)})
            log.info("bulk_recompute complete", extra={"done": done})

        # Endpoint is async, so there's always a running loop here. Store the
        # task ref to keep it alive (create_task only holds a weak ref).
        task = asyncio.create_task(_run_recompute())
        _BG_TASKS.add(task)
        task.add_done_callback(_BG_TASKS.discard)

    return BulkRecomputeResponse(queued=len(card_specs))


# ---------------------------------------------------------------------------
# POST /api/bulk/move-to-bulk-lot
# ---------------------------------------------------------------------------

class BulkLotBody(BaseModel):
    ids: List[int]
    lot_label: str


class BulkLotResponse(BaseModel):
    moved: int


@router.post("/bulk/move-to-bulk-lot", response_model=BulkLotResponse)
def bulk_move_to_lot(body: BulkLotBody) -> BulkLotResponse:
    """Set status = 'Bulk' and append a lot_label note."""
    moved = 0
    with _log.step(log, "bulk_move_to_lot", count=len(body.ids), lot_label=body.lot_label):
        with session_scope() as s:
            for cid in body.ids:
                c = s.get(models.Card, cid)
                if c is None:
                    continue
                c.status = "Bulk"
                lot_note = f"lot:{body.lot_label}"
                existing = c.notes or ""
                if lot_note not in existing:
                    c.notes = (existing + "\n" + lot_note).strip()
                from datetime import datetime
                c.updated_at = datetime.utcnow()
                s.add(c)
                moved += 1
    return BulkLotResponse(moved=moved)
