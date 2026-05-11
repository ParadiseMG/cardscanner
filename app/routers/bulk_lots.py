"""Bulk-lot proposer and management router.

Endpoints:
  GET  /api/bulk-lots/proposals          — clustered lot proposals (5-min cache)
  POST /api/bulk-lots/create             — create a BulkLot and link cards
  POST /api/bulk-lots/{lot_id}/list-on-ebay  — publish lot to eBay
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from app import models
from app.db import get_engine, session_scope
from app.services import bulk_lots as bl_svc
from app.utils import logger as _log

router = APIRouter()
log = _log.get(__name__)

# ---------------------------------------------------------------------------
# 5-minute in-memory cache for proposals
# ---------------------------------------------------------------------------
_proposals_cache: Optional[list[bl_svc.BulkLotProposal]] = None
_proposals_cache_ts: float = 0.0
_CACHE_TTL = 300.0  # seconds


def _get_cached_proposals() -> list[bl_svc.BulkLotProposal]:
    global _proposals_cache, _proposals_cache_ts
    now = time.monotonic()
    if _proposals_cache is not None and (now - _proposals_cache_ts) < _CACHE_TTL:
        return _proposals_cache

    # Recompute
    with Session(get_engine()) as s:
        cards = s.exec(select(models.Card)).all()

    _proposals_cache = bl_svc.cluster_for_lots(cards)
    _proposals_cache_ts = now
    log.info("bulk_lots.proposals_recomputed", extra={"count": len(_proposals_cache)})
    return _proposals_cache


def _invalidate_cache() -> None:
    global _proposals_cache, _proposals_cache_ts
    _proposals_cache = None
    _proposals_cache_ts = 0.0


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------

class ProposalOut(BaseModel):
    card_ids: list[int]
    cluster_label: str
    count: int
    estimated_value: float
    suggested_title: str
    suggested_price: float


class ProposalsResponse(BaseModel):
    proposals: list[ProposalOut]
    cached: bool


class CreateLotRequest(BaseModel):
    card_ids: list[int]
    label: str
    listing_title: Optional[str] = None
    price: Optional[float] = None


class CreateLotResponse(BaseModel):
    bulk_lot_id: int
    linked_cards: int


class ListOnEbayResponse(BaseModel):
    bulk_lot_id: int
    success: bool
    offer_id: Optional[str] = None
    listing_id: Optional[str] = None
    error: Optional[str] = None


# ---------------------------------------------------------------------------
# GET /api/bulk-lots/proposals
# ---------------------------------------------------------------------------

@router.get("/bulk-lots/proposals", response_model=ProposalsResponse)
def get_proposals() -> ProposalsResponse:
    """Return clustered lot proposals. Cached for 5 minutes."""
    now = time.monotonic()
    was_cached = (
        _proposals_cache is not None
        and (now - _proposals_cache_ts) < _CACHE_TTL
    )
    proposals = _get_cached_proposals()
    return ProposalsResponse(
        proposals=[
            ProposalOut(
                card_ids=p.card_ids,
                cluster_label=p.cluster_label,
                count=p.count,
                estimated_value=p.estimated_value,
                suggested_title=p.suggested_title,
                suggested_price=p.suggested_price,
            )
            for p in proposals
        ],
        cached=was_cached,
    )


# ---------------------------------------------------------------------------
# POST /api/bulk-lots/create
# ---------------------------------------------------------------------------

@router.post("/bulk-lots/create", response_model=CreateLotResponse)
def create_lot(req: CreateLotRequest) -> CreateLotResponse:
    """Create a BulkLot, set each card's status to Bulk and lot_id."""
    with session_scope() as s:
        lot = models.BulkLot(
            label=req.label,
            listing_title=req.listing_title,
            price=req.price,
            status="draft",
            created_at=datetime.utcnow(),
        )
        s.add(lot)
        s.flush()  # get lot.id

        linked = 0
        for cid in req.card_ids:
            card = s.get(models.Card, cid)
            if card is None:
                continue
            card.status = "Bulk"
            card.lot_id = lot.id
            card.updated_at = datetime.utcnow()
            s.add(card)
            linked += 1

        lot_id = lot.id

    # Invalidate cache — the landscape just changed
    _invalidate_cache()

    log.info("bulk_lots.create", extra={"lot_id": lot_id, "linked": linked})
    return CreateLotResponse(bulk_lot_id=lot_id, linked_cards=linked)


# ---------------------------------------------------------------------------
# POST /api/bulk-lots/{lot_id}/list-on-ebay
# ---------------------------------------------------------------------------
# Category choice:
#   213  = Sports Memorabilia, Cards & Fan Shop > Trading Cards > Baseball
#          (standard single-card category)
#   261330 = Sports Trading Card Lots
# We use 261330 for bulk lots since it targets multi-card listings specifically.
LOT_EBAY_CATEGORY = "261330"

@router.post("/bulk-lots/{lot_id}/list-on-ebay", response_model=ListOnEbayResponse)
async def list_on_ebay(lot_id: int) -> ListOnEbayResponse:
    """Draft an eBay listing for the bulk lot via the existing publish_listing path."""
    from app.services import ebay_listing

    with Session(get_engine()) as s:
        lot = s.get(models.BulkLot, lot_id)
        if lot is None:
            raise HTTPException(404, f"BulkLot {lot_id} not found")

        # Grab a representative card from the lot for the listing call
        linked_cards = s.exec(
            select(models.Card).where(models.Card.lot_id == lot_id)
        ).all()

    if not linked_cards:
        raise HTTPException(400, "No cards linked to this lot")

    # Build the listing draft — use a synthetic card-like object for the template
    # The representative card is used for image lookup; lot title/price override.
    rep_card = linked_cards[0]
    price = lot.price or sum(
        (c.comp_median_weighted or c.comp_median or c.est_value_raw or 0)
        for c in linked_cards
    ) * 0.7

    draft = {
        "title": lot.listing_title or f"Lot of {len(linked_cards)} Baseball Cards — {lot.label}",
        "description": (
            f"Bulk lot: {lot.label}. "
            f"Contains {len(linked_cards)} cards. "
            "All cards are raw (ungraded) unless otherwise noted."
        ),
        "price": round(price, 2),
        "currency": "USD",
        "format": "AUCTION",
        "duration": "DAYS_7",
        "category_id": LOT_EBAY_CATEGORY,
        "condition": "USED_EXCELLENT",
        "aspects": {
            "Sport": ["Baseball"],
            "Type": ["Baseball Card Lot"],
        },
    }

    try:
        result = await ebay_listing.publish_listing(rep_card, draft, publish=False)
    except Exception as exc:
        log.warning("bulk_lots.list_on_ebay error", extra={"lot_id": lot_id, "error": str(exc)})
        return ListOnEbayResponse(
            bulk_lot_id=lot_id, success=False, error=str(exc)
        )

    if result.success:
        with session_scope() as s:
            lot_row = s.get(models.BulkLot, lot_id)
            if lot_row:
                lot_row.status = "listed"
                if result.listing_id:
                    lot_row.ebay_listing_id = result.listing_id
                s.add(lot_row)

    log.info("bulk_lots.list_on_ebay", extra={
        "lot_id": lot_id,
        "success": result.success,
        "offer_id": result.offer_id,
    })

    return ListOnEbayResponse(
        bulk_lot_id=lot_id,
        success=result.success,
        offer_id=result.offer_id,
        listing_id=result.listing_id,
        error=result.error,
    )
