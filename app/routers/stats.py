"""Stats / achievements / action queue endpoints."""
from __future__ import annotations

from fastapi import APIRouter
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


@router.get("/action-queue")
def action_queue() -> dict:
    with Session(get_engine()) as s:
        review = s.exec(select(models.Card).where(models.Card.review_flagged == True)).all()
        grade  = s.exec(select(models.Card).where(models.Card.consider_grading == True,
                                                  models.Card.is_graded == False)).all()
        photo  = s.exec(select(models.Card).where(models.Card.needs_photo_verification == True)).all()
        def m(c: models.Card) -> dict:
            return {"id": c.id, "title": c.display_title(),
                    "value": c.comp_median or c.est_value_raw or 0}
        return {
            "needs_review": [m(c) for c in review],
            "consider_grading": [m(c) for c in grade],
            "needs_photo_verification": [m(c) for c in photo],
        }
