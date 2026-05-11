"""SQLite engine, session helper, schema bootstrap."""
from __future__ import annotations

from contextlib import contextmanager
from sqlmodel import SQLModel, Session, create_engine

from app.config import settings
from app import models  # noqa: F401  (registers tables)

_engine = create_engine(
    f"sqlite:///{settings.db_path}",
    echo=False,
    connect_args={"check_same_thread": False},
)


def init_db() -> None:
    SQLModel.metadata.create_all(_engine)
    _seed_hit_watchlist()


def get_engine():
    return _engine


@contextmanager
def session_scope():
    s = Session(_engine)
    try:
        yield s
        s.commit()
    except Exception:
        s.rollback()
        raise
    finally:
        s.close()


def session() -> Session:
    """Caller-managed session (for FastAPI deps)."""
    return Session(_engine)


# ---------------------------------------------------------------------------
# Hit Watchlist seed — distilled from Connor's existing Hit Watchlist tab.
# Patterns are case-insensitive substrings; player_pattern can be a partial.
# ---------------------------------------------------------------------------
_HIT_WATCHLIST_SEED = [
    # Junk-wax stars
    (1989, 1989, "Upper Deck", "Griffey", "1", 30.0, "1989 UD #1 Ken Griffey Jr RC"),
    (1986, 1986, "Donruss", "Canseco", "39", 8.0, "Canseco RC"),
    (1985, 1985, "Topps", "McGwire", "401", 12.0, "McGwire Olympic RC"),
    (1990, 1990, "Leaf", "Thomas", "300", 15.0, "Frank Thomas Leaf RC"),
    (1991, 1991, "Topps Desert Shield", None, None, 5.0, "Desert Shield variation"),
    (1989, 1989, "Topps Traded", "Smoltz", None, 5.0, "Smoltz RC"),
    (1990, 1990, "Bowman", "Thomas", None, 8.0, "Frank Thomas Bowman RC"),
    # Modern hits
    (2011, 2011, "Topps Update", "Trout", "US175", 800.0, "Mike Trout RC"),
    (2018, 2018, "Topps", "Acuna", None, 30.0, "Acuna Jr RC"),
    (2018, 2018, "Bowman Chrome", "Acuna", None, 80.0, "Acuna Bowman Chrome"),
    (2019, 2019, "Topps", "Tatis", "410", 25.0, "Tatis Jr RC"),
    (2020, 2020, "Bowman", "Witt", None, 40.0, "Bobby Witt Jr 1st Bowman"),
    (2020, 2020, "Bowman", "De La Cruz", None, 30.0, "Elly De La Cruz 1st Bowman"),
    (2023, 2023, "Bowman Chrome", "Holliday", None, 80.0, "Jackson Holliday 1st"),
    # Vintage
    (1900, 1979, None, "Mantle", None, 200.0, "Anything with Mantle is significant"),
    (1900, 1979, None, "Mays", None, 100.0, "Mays vintage"),
    (1900, 1979, None, "Aaron", None, 80.0, "Aaron vintage"),
    (1900, 1979, None, "Clemente", None, 80.0, "Clemente vintage"),
    # Auto/relic catch-alls
    (1990, 2099, "Auto", None, None, 25.0, "Any certified autograph"),
    (1990, 2099, "Patch", None, None, 25.0, "Any patch / relic"),
]


def _seed_hit_watchlist() -> None:
    from sqlmodel import select
    with session_scope() as s:
        existing = s.exec(select(models.HitWatchlistEntry)).first()
        if existing:
            return
        for ymin, ymax, sp, pp, cn, tv, reason in _HIT_WATCHLIST_SEED:
            s.add(models.HitWatchlistEntry(
                year_min=ymin, year_max=ymax,
                set_pattern=sp, player_pattern=pp, card_no=cn,
                typical_value=tv, reason=reason,
            ))
