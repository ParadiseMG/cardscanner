"""CRUD on the Card inventory."""
from __future__ import annotations

from typing import Optional, List
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session, select

from app import models, achievements
from app.db import get_engine

router = APIRouter()


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


@router.get("/inventory")
def list_cards(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    status: Optional[str] = None,
    hit_only: bool = False,
    needs_review: bool = False,
    q: Optional[str] = None,
) -> dict:
    with Session(get_engine()) as s:
        stmt = select(models.Card).order_by(models.Card.id.desc())
        if status:
            stmt = stmt.where(models.Card.status == status)
        if hit_only:
            stmt = stmt.where(models.Card.is_hit_watchlist == True)
        if needs_review:
            stmt = stmt.where(models.Card.review_flagged == True)
        rows = s.exec(stmt).all()
        if q:
            ql = q.lower()
            rows = [c for c in rows
                    if (c.player or "").lower().find(ql) >= 0
                    or (c.set_brand or "").lower().find(ql) >= 0]
        total = len(rows)
        page = rows[offset: offset + limit]
        return {
            "total": total,
            "items": [CardOut.from_db(c).model_dump() for c in page],
        }


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
