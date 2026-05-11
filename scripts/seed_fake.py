#!/usr/bin/env python3
"""Seed the DB with N synthetic cards for local performance testing.

Usage:
    DB_PATH=/tmp/r2a_seed.db python scripts/seed_fake.py 3000
    DB_PATH=/tmp/r2a_seed.db python scripts/seed_fake.py  # default 3000
"""
from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Must be set before any app imports so config picks up DB_PATH from env.
# When running via 'python scripts/seed_fake.py', the env var is already set
# by the caller. This is just a guard for interactive use.
import os
if "DB_PATH" not in os.environ:
    import tempfile
    _tmp = Path(tempfile.mkdtemp(prefix="cs_seed_", dir="/tmp"))
    os.environ["DB_PATH"] = str(_tmp / "cs.db")
    print(f"[seed] DB_PATH not set, using {os.environ['DB_PATH']}", file=sys.stderr)
os.environ.setdefault("LOCAL_XLSX_PATH", "")
os.environ.setdefault("GOOGLE_OAUTH_CLIENT_SECRETS", "/tmp/no_secret.json")
os.environ.setdefault("EBAY_APP_ID", "")
os.environ.setdefault("EBAY_CERT_ID", "")

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import sqlmodel as sm
from app.db import get_engine, session_scope
from app import models

# ---------------------------------------------------------------------------
# Data pools
# ---------------------------------------------------------------------------

PLAYERS = [
    "Mike Trout", "Ronald Acuna Jr", "Juan Soto", "Shohei Ohtani",
    "Julio Rodriguez", "Bobby Witt Jr", "Adley Rutschman", "Spencer Torkelson",
    "Ken Griffey Jr", "Derek Jeter", "Cal Ripken Jr", "Nolan Arenado",
    "Vladimir Guerrero Jr", "Bo Bichette", "Corbin Carroll", "Gunnar Henderson",
    "Jackson Holliday", "Elly De La Cruz", "Pete Crow-Armstrong", "Paul Skenes",
    "Frank Thomas", "Greg Maddux", "Randy Johnson", "Roger Clemens",
    "Barry Bonds", "Mark McGwire", "Jose Canseco", "Ryne Sandberg",
    "Ozzie Smith", "Wade Boggs", "Kirby Puckett", "Dave Winfield",
    "Nolan Ryan", "George Brett", "Robin Yount", "Dave Winfield",
    "Mickey Mantle", "Willie Mays", "Hank Aaron", "Roberto Clemente",
    "Sandy Koufax", "Bob Gibson", "Ted Williams", "Joe DiMaggio",
]

SET_BRANDS = [
    "Topps", "Topps Chrome", "Topps Update", "Topps Heritage",
    "Bowman", "Bowman Chrome", "Bowman Draft",
    "Upper Deck", "Upper Deck SP",
    "Donruss", "Donruss Optic",
    "Fleer", "Score", "Stadium Club",
    "Panini Prizm", "Select", "Mosaic",
    "Leaf", "Pacific", "Finest",
    "Topps Finest", "Topps Gold Label",
    "Topps Desert Shield", "Topps Traded",
    "O-Pee-Chee",
]

PARALLELS = [
    "Base", "Base", "Base", "Base", "Base",  # weight base heavily
    "Refractor", "Prizm", "Gold",
    "Holo", "Foil", "Rainbow Foil",
    "Blue /150", "Red /99", "Gold /50", "Orange /25",
    "Green /10", "Purple /5", "Superfractor /1",
]

CONDITIONS = ["NM", "NM-MT", "EX-MT", "EX", "VG-EX", "VG", "GD"]

STATUSES = [
    "Researching", "Researching", "Researching", "Researching",  # most cards
    "Ready", "Ready",
    "Listed", "Sold", "Bulk",
]

EBAY_STATUSES = [
    "not_listed", "not_listed", "not_listed", "not_listed",  # most cards
    "drafted", "active", "sold",
]

# Year pools by era
YEAR_POOLS = {
    "Vintage (pre-1986)": list(range(1952, 1986)),
    "Junk Wax (1986-1991)": list(range(1986, 1992)),
    "Transitional (1992-1999)": list(range(1992, 2000)),
    "Modern (2000-2014)": list(range(2000, 2015)),
    "Ultra-Modern (2015+)": list(range(2015, 2026)),
}

# Distribution of eras (weights)
ERA_WEIGHTS = {
    "Vintage (pre-1986)": 0.05,
    "Junk Wax (1986-1991)": 0.30,
    "Transitional (1992-1999)": 0.15,
    "Modern (2000-2014)": 0.25,
    "Ultra-Modern (2015+)": 0.25,
}


def _rand_era() -> str:
    eras = list(ERA_WEIGHTS.keys())
    weights = list(ERA_WEIGHTS.values())
    return random.choices(eras, weights=weights, k=1)[0]


def _rand_year(era: str) -> int:
    return random.choice(YEAR_POOLS[era])


def _rand_value() -> float | None:
    # ~20% have no comp
    if random.random() < 0.20:
        return None
    # Log-ish distribution: most cards are cheap
    r = random.random()
    if r < 0.60:
        return round(random.uniform(0.25, 5.0), 2)
    elif r < 0.85:
        return round(random.uniform(5.0, 30.0), 2)
    elif r < 0.95:
        return round(random.uniform(30.0, 150.0), 2)
    else:
        return round(random.uniform(150.0, 2000.0), 2)


def _make_card(i: int) -> models.Card:
    era = _rand_era()
    year = _rand_year(era)
    player = random.choice(PLAYERS)
    set_brand = random.choice(SET_BRANDS)
    parallel = random.choice(PARALLELS)
    comp_median = _rand_value()
    is_auto = random.random() < 0.05   # ~5% autographs
    is_relic = random.random() < 0.03  # ~3% relics
    is_graded = random.random() < 0.02 # ~2% graded
    is_hit = is_auto or is_relic or is_graded or (comp_median is not None and comp_median >= 50)

    # Spread created_at over the past 90 days for realistic stats
    days_ago = random.uniform(0, 90)
    created_at = datetime.utcnow() - timedelta(days=days_ago)

    return models.Card(
        year=year,
        set_brand=set_brand,
        player=player,
        card_no=str(random.randint(1, 999)),
        parallel=parallel,
        sport="Baseball",
        team=None,
        condition=random.choice(CONDITIONS),
        is_graded=is_graded,
        grade=f"PSA {random.randint(6, 10)}" if is_graded else None,
        is_autograph=is_auto,
        is_relic=is_relic,
        comp_median=comp_median,
        comp_low=round(comp_median * 0.7, 2) if comp_median else None,
        comp_high=round(comp_median * 1.4, 2) if comp_median else None,
        comp_count=random.randint(1, 20) if comp_median else 0,
        est_value_raw=comp_median,
        status=random.choice(STATUSES),
        ebay_status=random.choice(EBAY_STATUSES),
        is_hit_watchlist=is_hit,
        review_flagged=random.random() < 0.05,
        consider_grading=comp_median is not None and comp_median >= 30 and not is_graded,
        created_at=created_at,
        updated_at=created_at,
        front_hash=f"fakehash_{i:06d}",
    )


def seed(n: int = 3000) -> None:
    # Ensure schema is created and migrations applied
    from app.db import init_db
    init_db()

    print(f"[seed] Inserting {n} fake cards into {os.environ['DB_PATH']}...")
    batch_size = 500
    inserted = 0

    for batch_start in range(0, n, batch_size):
        batch_end = min(batch_start + batch_size, n)
        with session_scope() as s:
            for i in range(batch_start, batch_end):
                s.add(_make_card(i))
        inserted += (batch_end - batch_start)
        print(f"[seed]   {inserted}/{n} cards inserted")

    print(f"[seed] Done. {n} cards seeded.")


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 3000
    seed(n)
