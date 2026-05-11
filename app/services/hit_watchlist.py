"""Match a freshly-identified card against the seeded Hit Watchlist."""
from __future__ import annotations

from sqlmodel import Session, select

from app import models


def match(card: models.Card, session: Session) -> tuple[bool, str | None]:
    """Return (is_hit, reason). First match wins."""
    rows = session.exec(select(models.HitWatchlistEntry)).all()
    for r in rows:
        if r.year_min and (card.year or 0) < r.year_min:
            continue
        if r.year_max and (card.year or 9999) > r.year_max:
            continue
        if r.set_pattern and (card.set_brand or "").lower().find(r.set_pattern.lower()) < 0:
            # Special handling for 'Auto' / 'Patch' patterns -- check parallel too
            blob = " ".join(filter(None, [
                card.set_brand, card.parallel,
                "auto" if card.is_autograph else "",
                "patch" if card.is_relic else "",
            ])).lower()
            if r.set_pattern.lower() not in blob:
                continue
        if r.player_pattern and (card.player or "").lower().find(r.player_pattern.lower()) < 0:
            continue
        if r.card_no and (card.card_no or "").lower() != r.card_no.lower():
            continue
        return True, r.reason
    return False, None
