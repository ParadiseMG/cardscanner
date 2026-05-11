"""Sales router — A2: sync, summary, recent, manual-mark-sold.

Endpoints
---------
POST /api/sales/sync                   — fire-and-forget background sync
GET  /api/sales/summary                — P&L totals
GET  /api/sales/recent?n=20            — most recent sold cards
POST /api/sales/{listing_id}/mark-sold-manual
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from app import models, achievements
from app.db import get_engine, session_scope
from app.utils import logger as _log

router = APIRouter()
log = _log.get(__name__)

FEE_PCT_DEFAULT = 0.13


# ---------------------------------------------------------------------------
# POST /api/sales/sync
# ---------------------------------------------------------------------------

@router.post("/sales/sync")
async def trigger_sync(background_tasks: BackgroundTasks) -> dict:
    """Fire-and-forget eBay state sync. Returns immediately."""
    async def _run():
        try:
            from app.services.sales_sync import sync_all_active
            result = await sync_all_active()
            log.info("sales sync complete", extra=result)
        except Exception as exc:
            log.warning("sales sync background error", extra={"error": str(exc)})

    background_tasks.add_task(_run)
    return {"queued": True}


# ---------------------------------------------------------------------------
# GET /api/sales/summary
# ---------------------------------------------------------------------------

@router.get("/sales/summary")
def sales_summary() -> dict:
    """Return P&L totals broken out by month and channel."""
    with Session(get_engine()) as s:
        sold_cards = s.exec(
            select(models.Card).where(models.Card.status == "Sold")
        ).all()

        active_listings = s.exec(
            select(models.Listing).where(models.Listing.status == "active")
        ).all()

        draft_listings = s.exec(
            select(models.Listing).where(models.Listing.status == "draft")
        ).all()

    realized_total = 0.0
    fees_total = 0.0
    days_to_sell: list[float] = []

    by_month: dict[str, dict] = defaultdict(lambda: {
        "gross": 0.0, "fees": 0.0, "net": 0.0, "count": 0
    })
    by_channel: dict[str, dict] = defaultdict(lambda: {
        "gross": 0.0, "fees": 0.0, "net": 0.0, "count": 0
    })

    for card in sold_cards:
        sp = card.sold_price or 0.0
        fp = card.fee_pct or FEE_PCT_DEFAULT
        fee = round(sp * fp, 2)
        net = round(sp - fee, 2)

        realized_total += sp
        fees_total += fee

        # by_month key
        ts = card.sold_at or (
            datetime.combine(card.sold_date, datetime.min.time()) if card.sold_date else None
        )
        if ts:
            mk = ts.strftime("%Y-%m")
            by_month[mk]["gross"] = round(by_month[mk]["gross"] + sp, 2)
            by_month[mk]["fees"] = round(by_month[mk]["fees"] + fee, 2)
            by_month[mk]["net"] = round(by_month[mk]["net"] + net, 2)
            by_month[mk]["count"] += 1

            # sell-through days
            delta = (ts - card.created_at).total_seconds() / 86400
            if delta >= 0:
                days_to_sell.append(delta)

        # by_channel
        ch = card.sale_channel or card.channel or "eBay"
        by_channel[ch]["gross"] = round(by_channel[ch]["gross"] + sp, 2)
        by_channel[ch]["fees"] = round(by_channel[ch]["fees"] + fee, 2)
        by_channel[ch]["net"] = round(by_channel[ch]["net"] + net, 2)
        by_channel[ch]["count"] += 1

    realized_net = round(realized_total - fees_total, 2)
    realized_total = round(realized_total, 2)
    fees_total = round(fees_total, 2)

    active_value = round(sum((L.price or 0.0) for L in active_listings), 2)
    draft_value = round(sum((L.price or 0.0) for L in draft_listings), 2)

    avg_days = round(sum(days_to_sell) / len(days_to_sell), 1) if days_to_sell else None

    return {
        "realized_total": realized_total,
        "realized_net": realized_net,
        "fees_total": fees_total,
        "by_month": dict(by_month),
        "by_channel": dict(by_channel),
        "active_value": active_value,
        "draft_value": draft_value,
        "sell_through_days_avg": avg_days,
    }


# ---------------------------------------------------------------------------
# GET /api/sales/recent?n=20
# ---------------------------------------------------------------------------

@router.get("/sales/recent")
def recent_sales(n: int = 20) -> list[dict]:
    """Most recent sold cards ordered by sold_at DESC."""
    with Session(get_engine()) as s:
        cards = s.exec(
            select(models.Card)
            .where(models.Card.status == "Sold")
            .order_by(models.Card.sold_at.desc(), models.Card.id.desc())
            .limit(n)
        ).all()

        results = []
        for card in cards:
            sp = card.sold_price or 0.0
            fp = card.fee_pct or FEE_PCT_DEFAULT
            fee = round(sp * fp, 2)

            # Grab associated listing for offer/listing IDs
            listing = s.exec(
                select(models.Listing)
                .where(models.Listing.card_id == card.id)
                .order_by(models.Listing.id.desc())
            ).first()

            results.append({
                "card_id": card.id,
                "title": card.display_title(),
                "player": card.player,
                "year": card.year,
                "set_brand": card.set_brand,
                "sold_price": sp,
                "fees": fee,
                "net": round(sp - fee, 2),
                "sale_channel": card.sale_channel or card.channel or "eBay",
                "sold_at": card.sold_at.isoformat() if card.sold_at else None,
                "sold_date": card.sold_date.isoformat() if card.sold_date else None,
                "listing_id": listing.id if listing else None,
                "ebay_listing_id": card.ebay_listing_id,
                "acquisition_cost": card.acquisition_cost,
            })

    return results


# ---------------------------------------------------------------------------
# POST /api/sales/{listing_id}/mark-sold-manual
# (Replaces / supplements the old /api/listings/{id}/mark-sold)
# ---------------------------------------------------------------------------

class ManualSaleRequest(BaseModel):
    sold_price: float
    fees: Optional[float] = None
    channel: str = "eBay"
    sold_at: Optional[datetime] = None


@router.post("/sales/{listing_id}/mark-sold-manual")
def mark_sold_manual(listing_id: int, req: ManualSaleRequest) -> dict:
    """Manually mark a listing as sold — for non-eBay or un-synced sales.

    Updates both the Listing and its parent Card, then fires the xlsx/Sheets
    mirror in the background.
    """
    now = datetime.utcnow()
    sold_at = req.sold_at or now
    fees = req.fees if req.fees is not None else round(req.sold_price * FEE_PCT_DEFAULT, 2)

    card_id_for_mirror: Optional[int] = None
    with session_scope() as s:
        L = s.get(models.Listing, listing_id)
        if not L:
            raise HTTPException(404, "listing not found")

        L.status = "sold"
        L.sold_price = req.sold_price
        L.fees = fees
        L.sold_at = sold_at
        L.updated_at = now
        s.add(L)

        card = s.get(models.Card, L.card_id)
        if card:
            card.status = "Sold"
            card.sold_price = req.sold_price
            card.sold_date = sold_at.date()
            card.sold_at = sold_at
            card.sale_channel = req.channel
            card.ebay_status = "sold"
            card.fee_pct = fees / req.sold_price if req.sold_price else FEE_PCT_DEFAULT
            card.updated_at = now
            s.add(card)
            card_id_for_mirror = card.id

        new_unlocks = achievements.evaluate_unlocks(s, just_sold=True)

    # Mirror to xlsx + Sheets (best-effort, non-blocking)
    if card_id_for_mirror:
        try:
            from app.services import xlsx_mirror, sheets_sync
            # Open a fresh session — the previous session_scope has committed and closed
            with Session(get_engine()) as s2:
                fresh = s2.get(models.Card, card_id_for_mirror)
                if fresh:
                    try:
                        xlsx_mirror.update_sales_log(fresh)
                    except Exception as e:
                        log.warning("xlsx mirror failed", extra={"error": str(e)})
                    try:
                        sheets_sync.append_sale(fresh)
                    except Exception as e:
                        log.warning("sheets mirror failed", extra={"error": str(e)})
        except Exception as e:
            log.warning("mirror error", extra={"error": str(e)})

    return {
        "ok": True,
        "listing_id": listing_id,
        "sold_price": req.sold_price,
        "fees": fees,
        "net": round(req.sold_price - fees, 2),
        "channel": req.channel,
        "sold_at": sold_at.isoformat(),
        "unlocks": [u.code for u in new_unlocks],
    }
