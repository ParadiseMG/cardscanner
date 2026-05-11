"""B4: Suggestion engine — surfaces actionable next steps for each card.

GET /api/suggestions → {items: [{id, kind, title, value, reason}]}

Kinds:
  grade        — raw high-value cards worth submitting to PSA/SGC
  bulk         — sub-dollar singles better bundled into a bulk lot
  list         — Hit Watchlist matches that aren't yet listed
  reshoot      — cards with bad photo quality
  verify_comps — cards whose comp prices look like a bulk-lot fingerprint
"""
from __future__ import annotations

from typing import List, Optional

from fastapi import APIRouter
from pydantic import BaseModel
from sqlmodel import Session, select

from app import models
from app.db import get_engine
from app.utils import logger as _log

router = APIRouter()
log = _log.get(__name__)

_DELETED = "Deleted"
_MAX_ITEMS = 100


class SuggestionItem(BaseModel):
    id: int
    kind: str   # grade / bulk / list / reshoot / verify_comps
    title: str
    value: float
    reason: str


class SuggestionsResponse(BaseModel):
    items: List[SuggestionItem]


def _effective_value(card: models.Card) -> float:
    return card.comp_median_weighted or card.comp_median or card.est_value_raw or 0.0


def _grade_suggestions(cards: list[models.Card]) -> list[SuggestionItem]:
    out = []
    for card in cards:
        if card.is_graded:
            continue
        if card.status == _DELETED:
            continue
        val = _effective_value(card)
        if val < 80:
            continue
        out.append(SuggestionItem(
            id=card.id,
            kind="grade",
            title=card.display_title(),
            value=val,
            reason=f"Grading cost ~$25 PSA — projected post-grade value ${val:.2f} (assumes 9)",
        ))
    return out


def _bulk_suggestions(cards: list[models.Card]) -> list[SuggestionItem]:
    excluded = {"Bulk", "Sold", _DELETED}
    out = []
    for card in cards:
        if card.status in excluded:
            continue
        val = _effective_value(card)
        if val >= 1.0:
            continue
        out.append(SuggestionItem(
            id=card.id,
            kind="bulk",
            title=card.display_title(),
            value=val,
            reason="Sub-dollar single — bundle into a bulk lot",
        ))
    return out


def _list_suggestions(cards: list[models.Card]) -> list[SuggestionItem]:
    out = []
    for card in cards:
        if card.status == "Sold" or card.status == _DELETED:
            continue
        if not card.is_hit_watchlist:
            continue
        if card.ebay_status != "not_listed":
            continue
        val = _effective_value(card)
        out.append(SuggestionItem(
            id=card.id,
            kind="list",
            title=card.display_title(),
            value=val,
            reason="Hit Watchlist match — list it before it cools off",
        ))
    return out


def _reshoot_suggestions(cards: list[models.Card]) -> list[SuggestionItem]:
    bad_quality = {"blurry", "obstructed", "off_angle"}
    out = []
    for card in cards:
        if card.status == _DELETED:
            continue
        # photo_quality may not exist yet (added by Workstream A)
        quality = getattr(card, "photo_quality", None)
        if quality not in bad_quality:
            continue
        val = _effective_value(card)
        out.append(SuggestionItem(
            id=card.id,
            kind="reshoot",
            title=card.display_title(),
            value=val,
            reason="Photo quality flagged — better photo helps both ID and listing",
        ))
    return out


def _verify_comps_suggestions(cards: list[models.Card]) -> list[SuggestionItem]:
    out = []
    for card in cards:
        if card.status == _DELETED:
            continue
        if not card.comp_suspicious_bulk:
            continue
        val = _effective_value(card)
        reason = card.comp_suspicious_reason or "Comp prices look suspicious — verify before pricing"
        out.append(SuggestionItem(
            id=card.id,
            kind="verify_comps",
            title=card.display_title(),
            value=val,
            reason=reason,
        ))
    return out


@router.get("/suggestions", response_model=SuggestionsResponse)
def get_suggestions() -> SuggestionsResponse:
    with Session(get_engine()) as s:
        cards = s.exec(select(models.Card)).all()

    all_items: list[SuggestionItem] = []
    all_items.extend(_grade_suggestions(cards))
    all_items.extend(_bulk_suggestions(cards))
    all_items.extend(_list_suggestions(cards))
    all_items.extend(_reshoot_suggestions(cards))
    all_items.extend(_verify_comps_suggestions(cards))

    # Sort by value descending, cap at 100
    all_items.sort(key=lambda x: x.value, reverse=True)
    all_items = all_items[:_MAX_ITEMS]

    _log.step(log, "suggestions_compute", count=len(all_items))

    return SuggestionsResponse(items=all_items)
