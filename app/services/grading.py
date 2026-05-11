"""Grading submission packet builder + economics estimator.

Supports PSA and SGC CSV export formats and a simple projected-value model
so the UI can show 'Breakeven at PSA 8' before Connor ships a card off.
"""
from __future__ import annotations

import csv
import io
from typing import Literal, Optional

from app import models
from app.utils import logger as _log

log = _log.get(__name__)

# ---------------------------------------------------------------------------
# Grading multipliers — post-grade comp vs raw comp median.
# Source: rough market consensus; easy to tune here.
# PSA grade → multiplier on raw value
# ---------------------------------------------------------------------------
PSA_MULTIPLIERS: dict[int, float] = {
    6: 1.1,
    7: 1.25,
    8: 1.5,
    9: 3.0,
    10: 8.0,
}

SGC_MULTIPLIERS: dict[int, float] = {
    6: 1.05,
    7: 1.2,
    8: 1.4,
    9: 2.5,
    10: 6.0,
}

# ---------------------------------------------------------------------------
# Service-level costs (USD, per card)
# ---------------------------------------------------------------------------
PSA_COSTS: dict[str, float] = {
    "Value": 25.0,
    "Regular": 75.0,
    "Express": 150.0,
    "Super Express": 300.0,
    "Walk-Through": 600.0,
}

SGC_COSTS: dict[str, float] = {
    "Standard": 30.0,
    "Economy": 18.0,
    "Express": 65.0,
    "Super Express": 150.0,
}


def _effective_value(card: models.Card) -> float:
    """Return the best available raw comp value for the card."""
    if card.comp_median_weighted is not None:
        return card.comp_median_weighted
    if card.comp_median is not None:
        return card.comp_median
    if card.est_value_raw is not None:
        return card.est_value_raw
    return 0.0


# ---------------------------------------------------------------------------
# B1: CSV builders
# ---------------------------------------------------------------------------

# PSA bulk-submission column headers (matches PSA's template)
_PSA_HEADERS = [
    "Item", "Year", "Brand", "Player", "Variant", "Card #", "Service Level", "Notes"
]

# SGC bulk-submission column headers
_SGC_HEADERS = [
    "Item", "Year", "Brand", "Player", "Set Variation", "Card Number",
    "Declared Value", "Notes"
]


def build_psa_csv(cards: list[models.Card], service_level: str = "Value") -> str:
    """Build a PSA bulk-submission CSV string from a list of Card rows.

    Returns the raw CSV text ready to write to a file or stream.
    """
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    writer.writerow(_PSA_HEADERS)
    for i, card in enumerate(cards, start=1):
        writer.writerow([
            i,                                          # Item
            card.year or "",                            # Year
            card.set_brand or "",                       # Brand
            card.player or "",                          # Player
            card.parallel or "Base",                    # Variant
            card.card_no or "",                         # Card #
            service_level,                              # Service Level
            card.notes or "",                           # Notes
        ])
    log.info("grading.build_psa_csv", extra={
        "card_count": len(cards), "service_level": service_level
    })
    return buf.getvalue()


def build_sgc_csv(cards: list[models.Card]) -> str:
    """Build an SGC bulk-submission CSV string from a list of Card rows."""
    buf = io.StringIO()
    writer = csv.writer(buf, quoting=csv.QUOTE_MINIMAL)
    writer.writerow(_SGC_HEADERS)
    for i, card in enumerate(cards, start=1):
        writer.writerow([
            i,                                          # Item
            card.year or "",                            # Year
            card.set_brand or "",                       # Brand
            card.player or "",                          # Player
            card.parallel or "Base",                    # Set Variation
            card.card_no or "",                         # Card Number
            f"{_effective_value(card):.2f}",            # Declared Value
            card.notes or "",                           # Notes
        ])
    log.info("grading.build_sgc_csv", extra={"card_count": len(cards)})
    return buf.getvalue()


# ---------------------------------------------------------------------------
# B1: Economics estimator
# ---------------------------------------------------------------------------

def estimate_grading_economics(
    card: models.Card,
    service: Literal["PSA", "SGC"],
    expected_grade: int,
    service_level: str = "Value",
) -> dict:
    """Project the value and profit/loss of grading a card.

    Returns:
        {
            "raw_value": float,
            "cost": float,
            "projected_value": float,
            "projected_net": float,
            "breakeven_grade": int | None,
        }
    """
    raw_value = _effective_value(card)

    multipliers = PSA_MULTIPLIERS if service == "PSA" else SGC_MULTIPLIERS
    costs = PSA_COSTS if service == "PSA" else SGC_COSTS

    # Service level cost — default to lowest tier if not found
    default_sl = "Value" if service == "PSA" else "Standard"
    cost = costs.get(service_level, costs.get(default_sl, 25.0))

    # Projected value for the expected grade
    mult = multipliers.get(expected_grade, 1.0)
    projected_value = raw_value * mult
    projected_net = projected_value - cost - raw_value  # net gain above raw

    # Breakeven: smallest grade where (raw * mult) > cost + raw_value
    breakeven_grade: Optional[int] = None
    for g in sorted(multipliers):
        if raw_value * multipliers[g] > cost + raw_value:
            breakeven_grade = g
            break

    return {
        "raw_value": raw_value,
        "cost": cost,
        "projected_value": projected_value,
        "projected_net": projected_net,
        "breakeven_grade": breakeven_grade,
    }
