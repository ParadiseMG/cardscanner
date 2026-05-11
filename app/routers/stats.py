"""Stats / achievements / action queue endpoints."""
from __future__ import annotations

from datetime import datetime, timedelta

from fastapi import APIRouter
from pydantic import BaseModel
from sqlmodel import Session, select

from app import models, achievements
from app.db import get_engine

router = APIRouter()


@router.get("/stats")
def stats() -> dict:
    with Session(get_engine()) as s:
        return achievements.compute_stats(s)


@router.get("/achievements")
def all_achievements() -> dict:
    with Session(get_engine()) as s:
        return {"items": achievements.all_unlocks(s)}


@router.get("/achievements/pending")
def pending() -> dict:
    with Session(get_engine()) as s:
        return {"items": achievements.pending_celebrations(s)}


@router.post("/achievements/seen")
def mark_seen() -> dict:
    with Session(get_engine()) as s:
        n = achievements.mark_celebrations_seen(s)
        return {"marked": n}


@router.get("/insights")
def insights() -> dict:
    """Quick computed insights for the smart-insights bar."""
    with Session(get_engine()) as s:
        cards = s.exec(select(models.Card)).all()
        if not cards:
            return {"items": []}
        total = len(cards)
        hits = [c for c in cards if c.is_hit_watchlist]
        modern = [c for c in cards if (c.year or 0) >= 2015]
        modern_hits = [c for c in modern if c.is_hit_watchlist]
        bumps = [(c.comp_median or 0) - (c.est_value_raw or 0)
                 for c in cards if c.comp_median is not None]
        avg_bump = sum(bumps) / len(bumps) if bumps else 0

        items = []
        if modern:
            rate = len(modern_hits) / len(modern) * 100
            items.append({
                "icon": "✨",
                "headline": f"Hit rate from modern (2015+): {rate:.0f}%",
                "detail": f"{len(modern_hits)} hits in {len(modern)} modern cards.",
            })
        if avg_bump:
            sign = "+" if avg_bump >= 0 else ""
            items.append({
                "icon": "📈",
                "headline": f"Avg comp vs guess: {sign}${avg_bump:.2f}",
                "detail": "How much eBay sold prices have moved your initial estimate.",
            })
        if hits:
            top = max(hits, key=lambda c: c.comp_median or c.est_value_raw or 0)
            items.append({
                "icon": "🏆",
                "headline": f"Top hit: {top.display_title()} (${(top.comp_median or 0):.2f})",
                "detail": top.hit_reason or "On your Hit Watchlist.",
            })
        sample = [c for c in cards if c.consider_grading]
        if sample:
            items.append({
                "icon": "🎓",
                "headline": f"{len(sample)} cards worth considering for grading",
                "detail": "Comp median ≥ $30 and currently raw.",
            })
        return {"items": items}


# ---------------------------------------------------------------------------
# B8: GET /api/stats/last_24h
# ---------------------------------------------------------------------------

class Last24hResponse(BaseModel):
    scanned: int
    hits: int
    value_added: float
    ids: list[int]


@router.get("/stats/last_24h", response_model=Last24hResponse)
def last_24h() -> Last24hResponse:
    """Cards added in the last 24 hours with value summary."""
    cutoff = datetime.utcnow() - timedelta(hours=24)
    with Session(get_engine()) as s:
        cards = s.exec(
            select(models.Card).where(models.Card.created_at >= cutoff)
        ).all()

    def _effective(c: models.Card) -> float:
        return c.comp_median_weighted or c.comp_median or c.est_value_raw or 0.0

    # Sort by id ascending so ids list is deterministic
    cards_sorted = sorted(cards, key=lambda c: c.id)
    hits = [c for c in cards_sorted if c.is_hit_watchlist]
    value_added = sum(_effective(c) for c in cards_sorted)

    return Last24hResponse(
        scanned=len(cards_sorted),
        hits=len(hits),
        value_added=round(value_added, 2),
        ids=[c.id for c in cards_sorted],
    )


@router.get("/action-queue")
def action_queue() -> dict:
    with Session(get_engine()) as s:
        review = s.exec(select(models.Card).where(models.Card.review_flagged == True)).all()
        grade  = s.exec(select(models.Card).where(models.Card.consider_grading == True,
                                                  models.Card.is_graded == False)).all()
        photo  = s.exec(select(models.Card).where(models.Card.needs_photo_verification == True)).all()
        # B5: low comp confidence bucket — only cards worth manually re-checking (value ≥ $5)
        low_conf_raw = s.exec(
            select(models.Card).where(models.Card.comp_confidence == "low")
        ).all()

        def _effective(c: models.Card) -> float:
            return c.comp_median_weighted or c.comp_median or c.est_value_raw or 0.0

        low_conf = [c for c in low_conf_raw if _effective(c) >= 5.0]

        def m(c: models.Card) -> dict:
            return {"id": c.id, "title": c.display_title(),
                    "value": _effective(c)}
        return {
            "needs_review": [m(c) for c in review],
            "consider_grading": [m(c) for c in grade],
            "needs_photo_verification": [m(c) for c in photo],
            "low_comp_confidence": [m(c) for c in low_conf],
        }
