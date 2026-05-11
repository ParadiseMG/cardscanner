"""CRUD on the Card inventory."""
from __future__ import annotations

import json
from typing import Optional, List
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Header
from pydantic import BaseModel
from sqlmodel import Session, select

from app import models, achievements
from app.db import get_engine, session_scope
from app.config import UPLOAD_DIR
from app.utils import logger as _log

router = APIRouter()
log = _log.get(__name__)

# ---------------------------------------------------------------------------
# Value helper — coalesce(comp_median, est_value_raw, 0)
# ---------------------------------------------------------------------------

def _card_value(c: models.Card) -> float:
    if c.comp_median is not None:
        return c.comp_median
    if c.est_value_raw is not None:
        return c.est_value_raw
    return 0.0


# ---------------------------------------------------------------------------
# Output schema
# ---------------------------------------------------------------------------

class CardOut(BaseModel):
    id: int
    title: str
    year: Optional[int]
    set_brand: Optional[str]
    player: Optional[str]
    card_no: Optional[str]
    parallel: Optional[str]
    condition: Optional[str]
    is_graded: bool
    grade: Optional[str]
    is_autograph: bool
    is_relic: bool
    est_value_raw: Optional[float]
    comp_median: Optional[float]
    comp_low: Optional[float]
    comp_high: Optional[float]
    comp_count: int
    comp_url: Optional[str]
    status: str
    channel: Optional[str]
    is_hit_watchlist: bool
    hit_reason: Optional[str]
    notes: Optional[str]
    front_image: Optional[str]
    back_image: Optional[str]
    review_flagged: bool
    consider_grading: bool
    needs_photo_verification: bool
    ebay_status: str
    ebay_listing_id: Optional[str]
    era: str
    created_at: str
    # A6: Sale-status fields exposed in CardOut (Round 4)
    sold_at: Optional[str] = None
    sale_channel: Optional[str] = None
    acquisition_cost: Optional[float] = None

    @classmethod
    def from_db(cls, c: models.Card) -> "CardOut":
        return cls(
            id=c.id, title=c.display_title(), year=c.year, set_brand=c.set_brand,
            player=c.player, card_no=c.card_no, parallel=c.parallel,
            condition=c.condition, is_graded=c.is_graded, grade=c.grade,
            is_autograph=c.is_autograph, is_relic=c.is_relic,
            est_value_raw=c.est_value_raw, comp_median=c.comp_median,
            comp_low=c.comp_low, comp_high=c.comp_high, comp_count=c.comp_count,
            comp_url=c.comp_url, status=c.status, channel=c.channel,
            is_hit_watchlist=c.is_hit_watchlist, hit_reason=c.hit_reason,
            notes=c.notes, front_image=c.front_image, back_image=c.back_image,
            review_flagged=c.review_flagged, consider_grading=c.consider_grading,
            needs_photo_verification=c.needs_photo_verification,
            ebay_status=c.ebay_status, ebay_listing_id=c.ebay_listing_id,
            era=c.era(), created_at=c.created_at.isoformat(),
            # A6: sale-status fields
            sold_at=c.sold_at.isoformat() if c.sold_at else None,
            sale_channel=c.sale_channel,
            acquisition_cost=c.acquisition_cost,
        )


class CardPatch(BaseModel):
    year: Optional[int] = None
    set_brand: Optional[str] = None
    player: Optional[str] = None
    card_no: Optional[str] = None
    parallel: Optional[str] = None
    condition: Optional[str] = None
    is_graded: Optional[bool] = None
    grade: Optional[str] = None
    is_autograph: Optional[bool] = None
    is_relic: Optional[bool] = None
    est_value_raw: Optional[float] = None
    status: Optional[str] = None
    channel: Optional[str] = None
    notes: Optional[str] = None
    review_flagged: Optional[bool] = None
    needs_photo_verification: Optional[bool] = None


# ---------------------------------------------------------------------------
# A1: Extended GET /inventory
# ---------------------------------------------------------------------------

@router.get("/inventory")
def list_cards(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    # Legacy filters (Round 1)
    status: Optional[str] = None,
    hit_only: bool = False,
    needs_review: bool = False,
    q: Optional[str] = None,
    # A1: New filters
    sort: str = Query("recent", pattern="^(recent|value_desc|value_asc|year_desc|year_asc|player_az)$"),
    era: List[str] = Query(default=[]),
    ebay_status: Optional[str] = None,
    min_value: Optional[float] = None,
    max_value: Optional[float] = None,
    autograph: Optional[bool] = None,
    relic: Optional[bool] = None,
    graded: Optional[bool] = None,
    include_deleted: bool = False,
    # B7: server-side comp_confidence filter
    comp_confidence: Optional[str] = Query(default=None, pattern="^(high|medium|low)$"),
) -> dict:
    with Session(get_engine()) as s:
        stmt = select(models.Card)

        # Always exclude Deleted unless caller opts in
        if not include_deleted:
            stmt = stmt.where(models.Card.status != "Deleted")

        # Legacy status filter (still respected — but won't shadow Deleted exclusion)
        if status:
            stmt = stmt.where(models.Card.status == status)
        if hit_only:
            stmt = stmt.where(models.Card.is_hit_watchlist == True)
        if needs_review:
            stmt = stmt.where(models.Card.review_flagged == True)

        # A1: Boolean attribute filters
        if autograph is not None:
            stmt = stmt.where(models.Card.is_autograph == autograph)
        if relic is not None:
            stmt = stmt.where(models.Card.is_relic == relic)
        if graded is not None:
            stmt = stmt.where(models.Card.is_graded == graded)

        # A1: eBay status
        if ebay_status is not None:
            stmt = stmt.where(models.Card.ebay_status == ebay_status)

        # B7: comp confidence filter (server-side)
        if comp_confidence is not None:
            stmt = stmt.where(models.Card.comp_confidence == comp_confidence)

        # Fetch all matching rows (text search and era filter need Python-side eval)
        rows = s.exec(stmt).all()

        # A1: Text search (Python-side — keeps search feeling fast on 3k rows)
        if q:
            ql = q.lower()
            rows = [c for c in rows
                    if (c.player or "").lower().find(ql) >= 0
                    or (c.set_brand or "").lower().find(ql) >= 0]

        # A1: Era filter (multi-valued)
        if era:
            era_set = set(era)
            rows = [c for c in rows if c.era() in era_set]

        # A1: Value range filter
        if min_value is not None:
            rows = [c for c in rows if _card_value(c) >= min_value]
        if max_value is not None:
            rows = [c for c in rows if _card_value(c) <= max_value]

        # A1: Sort
        if sort == "recent":
            rows.sort(key=lambda c: c.id, reverse=True)
        elif sort == "value_desc":
            rows.sort(key=_card_value, reverse=True)
        elif sort == "value_asc":
            rows.sort(key=_card_value)
        elif sort == "year_desc":
            rows.sort(key=lambda c: c.year or 0, reverse=True)
        elif sort == "year_asc":
            rows.sort(key=lambda c: c.year or 0)
        elif sort == "player_az":
            rows.sort(key=lambda c: (c.player or "").lower())

        total = len(rows)
        page = rows[offset: offset + limit]
        return {
            "total": total,
            "items": [CardOut.from_db(c).model_dump() for c in page],
        }


# ---------------------------------------------------------------------------
# A2: GET /inventory/facets
# ---------------------------------------------------------------------------

class FacetsResponse(BaseModel):
    eras: dict
    ebay_status: dict
    value_buckets: dict
    tags: dict


@router.get("/inventory/facets", response_model=FacetsResponse)
def get_facets() -> FacetsResponse:
    """Aggregate counts for the filter sidebar — single pass over Card table."""
    with Session(get_engine()) as s:
        with _log.step(log, "facets_scan"):
            rows = s.exec(
                select(models.Card).where(models.Card.status != "Deleted")
            ).all()

        eras: dict[str, int] = {}
        ebay_status_counts: dict[str, int] = {}
        value_buckets: dict[str, int] = {
            "0-1": 0, "1-10": 0, "10-50": 0, "50-200": 0, "200+": 0
        }
        tags: dict[str, int] = {
            "autograph": 0, "relic": 0, "graded": 0, "hit": 0
        }

        for c in rows:
            # Era
            e = c.era()
            eras[e] = eras.get(e, 0) + 1

            # eBay status
            es = c.ebay_status or "not_listed"
            ebay_status_counts[es] = ebay_status_counts.get(es, 0) + 1

            # Value bucket
            v = _card_value(c)
            if v < 1:
                value_buckets["0-1"] += 1
            elif v < 10:
                value_buckets["1-10"] += 1
            elif v < 50:
                value_buckets["10-50"] += 1
            elif v < 200:
                value_buckets["50-200"] += 1
            else:
                value_buckets["200+"] += 1

            # Tags
            if c.is_autograph:
                tags["autograph"] += 1
            if c.is_relic:
                tags["relic"] += 1
            if c.is_graded:
                tags["graded"] += 1
            if c.is_hit_watchlist:
                tags["hit"] += 1

    return FacetsResponse(
        eras=eras,
        ebay_status=ebay_status_counts,
        value_buckets=value_buckets,
        tags=tags,
    )


# ---------------------------------------------------------------------------
# Existing endpoints (unchanged)
# ---------------------------------------------------------------------------

@router.get("/inventory/recent")
def recent(n: int = 10) -> list[dict]:
    with Session(get_engine()) as s:
        rows = s.exec(
            select(models.Card).order_by(models.Card.id.desc()).limit(n)
        ).all()
        return [CardOut.from_db(c).model_dump() for c in rows]


@router.get("/inventory/{card_id}")
def get_card(card_id: int) -> dict:
    with Session(get_engine()) as s:
        c = s.get(models.Card, card_id)
        if not c:
            raise HTTPException(404, "card not found")
        return CardOut.from_db(c).model_dump()


@router.patch("/inventory/{card_id}")
def patch_card(card_id: int, patch: CardPatch) -> dict:
    with Session(get_engine()) as s:
        c = s.get(models.Card, card_id)
        if not c:
            raise HTTPException(404, "card not found")
        for k, v in patch.model_dump(exclude_unset=True).items():
            setattr(c, k, v)
        c.updated_at = datetime.utcnow()
        s.add(c); s.commit(); s.refresh(c)
        return CardOut.from_db(c).model_dump()


@router.delete("/inventory/{card_id}")
def delete_card(card_id: int) -> dict:
    with Session(get_engine()) as s:
        c = s.get(models.Card, card_id)
        if not c:
            raise HTTPException(404, "card not found")
        s.delete(c); s.commit()
        return {"deleted": card_id}


# ---------------------------------------------------------------------------
# B5: POST /inventory/{card_id}/reidentify
# ---------------------------------------------------------------------------

# Fields that Claude returns which we may merge back into the Card row.
# user_overrides stores a JSON list of field names the user has locked.
_REIDENTIFY_FIELDS = (
    "year", "set_brand", "player", "card_no", "parallel", "sport", "team",
    "condition", "is_graded", "grade", "is_autograph", "is_relic",
    "is_rookie", "is_serial_numbered", "serial_print_run", "photo_quality",
    "notes",
)


@router.post("/inventory/{card_id}/reidentify")
async def reidentify_card(
    card_id: int,
    x_anthropic_key: Optional[str] = Header(default=None),
) -> dict:
    """Re-run Claude vision on the card's images and merge the result.

    Fields listed in Card.user_overrides are skipped — user edits win.
    Returns the updated CardOut.
    """
    from app.services import claude_vision

    with Session(get_engine()) as s:
        card = s.get(models.Card, card_id)
        if not card:
            raise HTTPException(404, "card not found")

        front_fname = card.front_image
        back_fname = card.back_image
        user_overrides: list[str] = []
        if card.user_overrides:
            try:
                user_overrides = json.loads(card.user_overrides)
            except (json.JSONDecodeError, TypeError):
                user_overrides = []

    if not front_fname:
        raise HTTPException(400, "card has no front_image to re-identify")

    front_path = UPLOAD_DIR / front_fname
    back_path = UPLOAD_DIR / back_fname if back_fname else None

    if not front_path.exists():
        raise HTTPException(400, f"front_image file not found: {front_fname}")

    api_key = x_anthropic_key or None

    with _log.step(log, "reidentify", card_id=card_id):
        ident = await claude_vision.identify_card_async(
            str(front_path),
            str(back_path) if back_path else None,
            api_key_override=api_key,
        )

    # Merge result into the card, skipping user_overrides fields
    ident_dict = ident.to_dict()

    with session_scope() as s:
        card = s.get(models.Card, card_id)
        if not card:
            raise HTTPException(404, "card not found")

        for field_name in _REIDENTIFY_FIELDS:
            if field_name in user_overrides:
                continue  # user has locked this field
            new_val = ident_dict.get(field_name)
            if new_val is not None:
                setattr(card, field_name, new_val)

        # Update JSON-encoded vision fields
        if "field_confidence" not in user_overrides and ident.field_confidence:
            import json as _json
            card.low_confidence_fields = _json.dumps(ident.low_confidence_fields)
            card.condition_signals = _json.dumps(ident.condition_signals)

        card.review_flagged = ident.review_flagged
        card.updated_at = datetime.utcnow()
        s.add(card)
        # refresh inside scope
        s.flush()
        result = CardOut.from_db(card)

    log.info("reidentify complete", extra={"card_id": card_id})
    return result.model_dump()
