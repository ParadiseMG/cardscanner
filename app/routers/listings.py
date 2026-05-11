"""eBay listings: draft preview / publish / list / per-card status."""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlmodel import Session, select

from app import models, achievements
from app.config import settings
from app.db import get_engine
from app.services import ebay_listing

router = APIRouter()


class PublishRequest(BaseModel):
    card_id: int
    overrides: Optional[dict] = None
    publish: bool = False  # default = draft only


class BulkPublishRequest(BaseModel):
    card_ids: list[int]
    publish: bool = False


@router.get("/listings/preview/{card_id}")
def preview(card_id: int) -> dict:
    with Session(get_engine()) as s:
        card = s.get(models.Card, card_id)
        if not card:
            raise HTTPException(404, "card not found")
        return ebay_listing.build_listing_template(card)


@router.get("/listings/policy-check")
async def policy_check() -> dict:
    if not ebay_listing.is_configured():
        return {"ready": False, "missing": ["app_credentials"], "env": settings.ebay_env}
    pc = await ebay_listing.check_seller_policies()
    return {"ready": pc.ready, "missing": pc.missing, "env": settings.ebay_env,
            "fulfillment_id": pc.fulfillment_id,
            "payment_id": pc.payment_id, "return_id": pc.return_id}


@router.post("/listings/publish")
async def publish(req: PublishRequest) -> dict:
    with Session(get_engine()) as s:
        card = s.get(models.Card, req.card_id)
        if not card:
            raise HTTPException(404, "card not found")
        draft = ebay_listing.build_listing_template(card)
        if req.overrides:
            draft.update(req.overrides)

        result = await ebay_listing.publish_listing(card, draft, publish=req.publish)

        listing = models.Listing(
            card_id=card.id,
            environment=settings.ebay_env,
            sku=result.sku,
            offer_id=result.offer_id,
            listing_id=result.listing_id,
            title=draft["title"],
            format=draft["format"],
            price=draft["price"],
            duration=draft["duration"],
            category_id=draft["category_id"],
            description=draft["description"],
            status="active" if result.listing_id else ("draft" if result.success else "error"),
            error_message=result.error,
        )
        s.add(listing); s.commit(); s.refresh(listing)

        card.ebay_status = listing.status
        card.ebay_offer_id = result.offer_id
        card.ebay_listing_id = result.listing_id
        card.updated_at = datetime.utcnow()
        s.add(card); s.commit()

        new = achievements.evaluate_unlocks(s, just_listed=True)

        return {
            "success": result.success,
            "error": result.error,
            "sku": result.sku,
            "offer_id": result.offer_id,
            "listing_id": result.listing_id,
            "status": listing.status,
            "unlocks": [u.code for u in new],
        }


@router.post("/listings/publish-bulk")
async def publish_bulk(req: BulkPublishRequest) -> dict:
    results = []
    for cid in req.card_ids:
        r = await publish(PublishRequest(card_id=cid, publish=req.publish))
        results.append({"card_id": cid, **r})
    return {"results": results}


@router.get("/listings")
def list_all(status: Optional[str] = None) -> dict:
    with Session(get_engine()) as s:
        stmt = select(models.Listing).order_by(models.Listing.id.desc())
        if status:
            stmt = stmt.where(models.Listing.status == status)
        rows = s.exec(stmt).all()
        out = []
        for L in rows:
            card = s.get(models.Card, L.card_id)
            out.append({
                "id": L.id, "card_id": L.card_id,
                "card_title": card.display_title() if card else None,
                "title": L.title, "format": L.format, "price": L.price,
                "status": L.status, "environment": L.environment,
                "sku": L.sku, "offer_id": L.offer_id, "listing_id": L.listing_id,
                "sold_price": L.sold_price, "fees": L.fees,
                "created_at": L.created_at.isoformat(),
                "error_message": L.error_message,
            })
        return {"items": out}


class SaleRequest(BaseModel):
    sold_price: float
    fees: Optional[float] = None


@router.post("/listings/{listing_id}/mark-sold")
# DEPRECATED (Round 4): prefer POST /api/sales/{listing_id}/mark-sold-manual
# which also supports sale_channel, sold_at, and fires the xlsx/Sheets mirrors.
# This route is kept for backward compatibility.
def mark_sold(listing_id: int, req: SaleRequest) -> dict:
    with Session(get_engine()) as s:
        L = s.get(models.Listing, listing_id)
        if not L:
            raise HTTPException(404, "listing not found")
        L.status = "sold"
        L.sold_price = req.sold_price
        L.fees = req.fees if req.fees is not None else round(req.sold_price * 0.13, 2)
        L.sold_at = datetime.utcnow()
        s.add(L)
        card = s.get(models.Card, L.card_id)
        if card:
            card.status = "Sold"
            card.sold_price = req.sold_price
            card.fee_pct = (L.fees / req.sold_price) if req.sold_price else 0.13
            card.ebay_status = "sold"
            s.add(card)
        s.commit()
        new = achievements.evaluate_unlocks(s, just_sold=True)
        return {"ok": True, "unlocks": [u.code for u in new]}
