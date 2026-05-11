"""Achievement catalog + unlock evaluator + insights aggregator."""
from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import date, timedelta
from typing import Callable, Iterable, Optional

from sqlmodel import Session, select, func

from app import models


@dataclass
class Achievement:
    code: str
    title: str
    description: str
    icon: str          # emoji
    tier: str = "bronze"  # bronze / silver / gold / legendary
    confetti: bool = False  # play big celebration on unlock


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------
CATALOG: list[Achievement] = [
    Achievement("first_scan", "First Scan", "Cataloged your very first card.", "🎴", "bronze"),
    Achievement("ten_cards",  "Getting Started", "Cataloged 10 cards.", "📦", "bronze"),
    Achievement("hundred_cards", "Century Club", "Cataloged 100 cards.", "💯", "silver"),
    Achievement("five_hundred_cards", "Box Buster", "Cataloged 500 cards.", "📚", "gold"),
    Achievement("thousand_cards", "Bulk Boss", "Cataloged 1,000 cards.", "🏆", "gold", confetti=True),
    Achievement("first_hit", "First Hit Watchlist Card", "Pulled a card off your Hit Watchlist.", "🎯", "silver", confetti=True),
    Achievement("auto_hunter", "Auto Hunter", "Found your first certified autograph.", "✍️", "silver", confetti=True),
    Achievement("relic_hunter", "Relic Hunter", "Found your first relic / patch.", "🧵", "silver"),
    Achievement("vintage_hunter", "Vintage Hunter", "Found a card older than 1980.", "🕰️", "silver"),
    Achievement("junk_wax_survivor", "Junk Wax Survivor", "Cataloged 100 cards from 1986–1991.", "🗑️", "bronze"),
    Achievement("modern_master", "Modern Master", "Cataloged 100 cards from 2015+.", "✨", "silver"),
    Achievement("first_100", "$100 Cataloged", "Total estimated value passed $100.", "💵", "bronze"),
    Achievement("first_500", "$500 Cataloged", "Total estimated value passed $500.", "💸", "silver"),
    Achievement("first_1k", "$1,000 Cataloged", "Total estimated value passed $1,000.", "💰", "gold", confetti=True),
    Achievement("first_5k", "$5,000 Cataloged", "Total estimated value passed $5,000.", "🏦", "legendary", confetti=True),
    Achievement("big_hit", "Big Hit", "Cataloged a card valued at $100+.", "🚀", "gold", confetti=True),
    Achievement("huge_hit", "Huge Hit", "Cataloged a card valued at $500+.", "🐳", "legendary", confetti=True),
    Achievement("streak_3", "3-Day Streak", "Added cards 3 days in a row.", "🔥", "bronze"),
    Achievement("streak_7", "7-Day Streak", "Added cards 7 days in a row.", "🔥🔥", "silver"),
    Achievement("streak_30", "30-Day Streak", "Added cards 30 days in a row.", "🔥🔥🔥", "gold", confetti=True),
    Achievement("first_ebay_listing", "First eBay Listing", "Drafted your first listing.", "📤", "silver"),
    Achievement("first_ebay_sale", "First eBay Sale", "Sold your first card on eBay.", "💼", "gold", confetti=True),
    Achievement("ebay_100_sold", "$100 in eBay Sales", "Realized $100 in eBay sales.", "💵", "silver"),
    Achievement("ebay_1k_sold", "$1K in eBay Sales", "Realized $1,000 in eBay sales.", "🤑", "gold", confetti=True),
]
BY_CODE: dict[str, Achievement] = {a.code: a for a in CATALOG}


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------
def _streak_length(session: Session) -> int:
    rows = session.exec(
        select(models.DailyActivity).order_by(models.DailyActivity.day.desc())
    ).all()
    if not rows:
        return 0
    today = date.today()
    streak = 0
    expected = today
    for r in rows:
        if r.day == expected:
            streak += 1
            expected = expected - timedelta(days=1)
        elif r.day < expected:
            break
    return streak


def evaluate_unlocks(session: Session, *, just_added_card: Optional[models.Card] = None,
                     just_listed: bool = False, just_sold: bool = False) -> list[Achievement]:
    """Compare current state vs already-unlocked codes; return new unlocks."""
    have_codes = {u.code for u in session.exec(select(models.AchievementUnlock)).all()}
    new: list[Achievement] = []

    cards = session.exec(select(models.Card)).all()
    n_cards = len(cards)
    total_value = sum((c.comp_median or c.est_value_raw or 0.0) for c in cards)
    junk_wax = sum(1 for c in cards if 1986 <= (c.year or 0) <= 1991)
    modern   = sum(1 for c in cards if (c.year or 0) >= 2015)

    counters = [
        ("first_scan", n_cards >= 1),
        ("ten_cards", n_cards >= 10),
        ("hundred_cards", n_cards >= 100),
        ("five_hundred_cards", n_cards >= 500),
        ("thousand_cards", n_cards >= 1000),
        ("first_100", total_value >= 100),
        ("first_500", total_value >= 500),
        ("first_1k", total_value >= 1000),
        ("first_5k", total_value >= 5000),
        ("junk_wax_survivor", junk_wax >= 100),
        ("modern_master", modern >= 100),
    ]

    if just_added_card:
        c = just_added_card
        if c.is_hit_watchlist:
            counters.append(("first_hit", True))
        if c.is_autograph:
            counters.append(("auto_hunter", True))
        if c.is_relic:
            counters.append(("relic_hunter", True))
        if (c.year or 9999) < 1980:
            counters.append(("vintage_hunter", True))
        v = c.comp_median or c.est_value_raw or 0
        if v >= 100:
            counters.append(("big_hit", True))
        if v >= 500:
            counters.append(("huge_hit", True))

    streak = _streak_length(session)
    counters += [
        ("streak_3", streak >= 3),
        ("streak_7", streak >= 7),
        ("streak_30", streak >= 30),
    ]

    if just_listed:
        counters.append(("first_ebay_listing", True))

    if just_sold:
        sold_total = sum(c.sold_price or 0 for c in cards if c.status == "Sold")
        counters += [
            ("first_ebay_sale", True),
            ("ebay_100_sold", sold_total >= 100),
            ("ebay_1k_sold", sold_total >= 1000),
        ]

    for code, cond in counters:
        if cond and code not in have_codes and code in BY_CODE:
            ach = BY_CODE[code]
            session.add(models.AchievementUnlock(code=code))
            new.append(ach)
            have_codes.add(code)
    if new:
        session.commit()
    return new


def all_unlocks(session: Session) -> list[dict]:
    have = session.exec(select(models.AchievementUnlock)).all()
    have_codes = {u.code: u.unlocked_at for u in have}
    out = []
    for ach in CATALOG:
        d = asdict(ach)
        d["unlocked"] = ach.code in have_codes
        d["unlocked_at"] = have_codes[ach.code].isoformat() if ach.code in have_codes else None
        out.append(d)
    return out


def pending_celebrations(session: Session) -> list[dict]:
    rows = session.exec(
        select(models.AchievementUnlock).where(models.AchievementUnlock.seen == False)
    ).all()
    out = []
    for r in rows:
        a = BY_CODE.get(r.code)
        if a:
            out.append({**asdict(a), "unlocked_at": r.unlocked_at.isoformat()})
    return out


def mark_celebrations_seen(session: Session) -> int:
    rows = session.exec(
        select(models.AchievementUnlock).where(models.AchievementUnlock.seen == False)
    ).all()
    for r in rows:
        r.seen = True
        session.add(r)
    session.commit()
    return len(rows)


# ---------------------------------------------------------------------------
# Insights / dashboard stats
# ---------------------------------------------------------------------------
def compute_stats(session: Session) -> dict:
    cards = session.exec(select(models.Card)).all()
    n = len(cards)

    def value_of(c: models.Card) -> float:
        return c.comp_median or c.est_value_raw or 0.0

    total_value = sum(value_of(c) for c in cards)
    high = max(cards, key=value_of, default=None)
    eras: dict[str, int] = {}
    for c in cards:
        eras[c.era()] = eras.get(c.era(), 0) + 1

    players: dict[str, int] = {}
    for c in cards:
        if c.player:
            players[c.player] = players.get(c.player, 0) + 1
    top_players = sorted(players.items(), key=lambda x: -x[1])[:5]

    hits = sum(1 for c in cards if c.is_hit_watchlist)
    hit_rate = (hits / n) if n else 0.0

    sold = [c for c in cards if c.status == "Sold"]
    realized = sum(c.sold_price or 0 for c in sold)
    fees = sum((c.sold_price or 0) * (c.fee_pct or 0.13) for c in sold)
    net = realized - fees

    streak = _streak_length(session)

    listings_total = session.exec(select(func.count(models.Listing.id))).one()  # type: ignore
    listings_active = session.exec(
        select(func.count(models.Listing.id)).where(models.Listing.status == "active")
    ).one()  # type: ignore
    listings_sold = session.exec(
        select(func.count(models.Listing.id)).where(models.Listing.status == "sold")
    ).one()  # type: ignore

    needs_review = sum(1 for c in cards if c.review_flagged)
    consider_grading = sum(1 for c in cards if c.consider_grading)

    return {
        "total_cards": n,
        "total_value": round(total_value, 2),
        "highest_value_card": {
            "id": high.id if high else None,
            "title": high.display_title() if high else None,
            "value": round(value_of(high), 2) if high else 0,
        },
        "era_distribution": eras,
        "top_players": [{"player": p, "count": c} for p, c in top_players],
        "hit_rate": round(hit_rate * 100, 1),
        "hit_count": hits,
        "sold_realized": round(realized, 2),
        "sold_fees": round(fees, 2),
        "sold_net": round(net, 2),
        "streak_days": streak,
        "listings_total": listings_total,
        "listings_active": listings_active,
        "listings_sold": listings_sold,
        "action_queue": {
            "needs_review": needs_review,
            "consider_grading": consider_grading,
        },
    }


def record_daily_activity(session: Session) -> None:
    today = date.today()
    existing = session.exec(
        select(models.DailyActivity).where(models.DailyActivity.day == today)
    ).first()
    if existing:
        existing.cards_added += 1
        session.add(existing)
    else:
        session.add(models.DailyActivity(day=today, cards_added=1))
