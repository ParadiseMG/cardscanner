"""Bulk-lot clustering service.

Pure function `cluster_for_lots` groups sub-$1 singles into proposed lots for
bulk eBay listings. Designed to be testable without a DB session.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from app import models
from app.utils import logger as _log

log = _log.get(__name__)

# Max cards per proposed lot (eBay buyers don't want a 500-card box)
LOT_MAX_CARDS = 100


@dataclass
class BulkLotProposal:
    card_ids: list[int]
    cluster_label: str
    count: int
    estimated_value: float   # sum of effective comp medians
    suggested_title: str
    suggested_price: float   # sum_of_comps * 0.7


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _effective_value(card: models.Card) -> float:
    if card.comp_median_weighted is not None:
        return card.comp_median_weighted
    if card.comp_median is not None:
        return card.comp_median
    if card.est_value_raw is not None:
        return card.est_value_raw
    return 0.0


def _decade(year: Optional[int]) -> str:
    if not year:
        return "Unknown"
    return f"{(year // 10) * 10}s"


def _era(year: Optional[int]) -> str:
    y = year or 0
    if y == 0:
        return "Unknown era"
    if y < 1986:
        return "Vintage (pre-1986)"
    if y < 1992:
        return "Junk Wax (1986-1991)"
    if y < 2000:
        return "Transitional (1992-1999)"
    if y < 2015:
        return "Modern (2000-2014)"
    return "Ultra-Modern (2015+)"


def _suggested_title(label: str, card_group: list[models.Card]) -> str:
    """Build a lot title using the 3 highest-value player names."""
    sorted_by_val = sorted(card_group, key=_effective_value, reverse=True)
    # Collect up to 3 distinct player names
    seen: set[str] = set()
    reps: list[str] = []
    for c in sorted_by_val:
        name = (c.player or "").strip()
        if name and name not in seen:
            seen.add(name)
            reps.append(name)
        if len(reps) == 3:
            break
    rep_str = ", ".join(reps) if reps else "various players"
    return f"Lot of {len(card_group)} {label} commons — {rep_str}"


def _make_proposals(
    card_group: list[models.Card],
    label: str,
) -> list[BulkLotProposal]:
    """Chunk a group into ≤LOT_MAX_CARDS proposals."""
    proposals = []
    # Sort by value desc so higher-value cards go in the first lot
    sorted_group = sorted(card_group, key=_effective_value, reverse=True)
    for start in range(0, len(sorted_group), LOT_MAX_CARDS):
        chunk = sorted_group[start: start + LOT_MAX_CARDS]
        total_val = sum(_effective_value(c) for c in chunk)
        proposals.append(BulkLotProposal(
            card_ids=[c.id for c in chunk],
            cluster_label=label,
            count=len(chunk),
            estimated_value=total_val,
            suggested_title=_suggested_title(label, chunk),
            suggested_price=round(total_val * 0.7, 2),
        ))
    return proposals


# ---------------------------------------------------------------------------
# B3: Main clustering function
# ---------------------------------------------------------------------------

def cluster_for_lots(cards: list[models.Card]) -> list[BulkLotProposal]:
    """Group sub-$1 cards into proposed bulk lots.

    Clustering priority:
      1. (year, set_brand)  — "1989 Donruss"
      2. (decade, sport)    — "1990s Baseball commons"
      3. (era,)             — fallback

    Returns proposals sorted by estimated_value DESC.
    """
    # Filter: sub-$1 effective value, not already in Bulk/Sold/Deleted
    candidates = [
        c for c in cards
        if _effective_value(c) < 1.0
        and c.status not in ("Bulk", "Sold", "Deleted")
    ]

    log.info("bulk_lots.cluster_for_lots", extra={
        "input": len(cards), "candidates": len(candidates)
    })

    if not candidates:
        return []

    all_proposals: list[BulkLotProposal] = []
    leftover: list[models.Card] = []

    # --- Pass 1: group by (year, set_brand) ---
    tier1: dict[tuple, list[models.Card]] = {}
    for c in candidates:
        if c.year and c.set_brand:
            key = (c.year, c.set_brand)
            tier1.setdefault(key, []).append(c)
        else:
            leftover.append(c)

    tier2_candidates: list[models.Card] = []
    for (year, brand), group in tier1.items():
        if len(group) >= 2:
            label = f"{year} {brand}"
            all_proposals.extend(_make_proposals(group, label))
        else:
            tier2_candidates.extend(group)

    # --- Pass 2: group by (decade, sport) ---
    tier2_candidates.extend(leftover)
    leftover = []
    tier2: dict[tuple, list[models.Card]] = {}
    for c in tier2_candidates:
        dec = _decade(c.year)
        sport = c.sport or "Baseball"
        key = (dec, sport)
        tier2.setdefault(key, []).append(c)

    tier3_candidates: list[models.Card] = []
    for (dec, sport), group in tier2.items():
        if len(group) >= 2:
            label = f"{dec} {sport} commons"
            all_proposals.extend(_make_proposals(group, label))
        else:
            tier3_candidates.extend(group)

    # --- Pass 3: group by era ---
    tier3: dict[str, list[models.Card]] = {}
    for c in tier3_candidates:
        era = _era(c.year)
        tier3.setdefault(era, []).append(c)

    for era, group in tier3.items():
        if len(group) >= 2:
            label = f"{era}"
            all_proposals.extend(_make_proposals(group, label))
        # Single-card groups are not worth a lot proposal

    # Sort by estimated_value DESC
    all_proposals.sort(key=lambda p: p.estimated_value, reverse=True)
    return all_proposals
