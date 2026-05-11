from sqlmodel import Session

from app import models
from app.db import get_engine, session_scope
from app.services import hit_watchlist


def test_griffey_rookie_matches():
    c = models.Card(year=1989, set_brand="Upper Deck", player="Ken Griffey Jr.", card_no="1")
    with Session(get_engine()) as s:
        is_hit, reason = hit_watchlist.match(c, s)
    assert is_hit is True
    assert reason and "Griffey" in reason


def test_random_modern_common_misses():
    c = models.Card(year=2024, set_brand="Topps Chrome", player="Random Guy")
    with Session(get_engine()) as s:
        is_hit, reason = hit_watchlist.match(c, s)
    assert is_hit is False


def test_autograph_matches_catchall():
    c = models.Card(year=2022, set_brand="Bowman", player="Some Prospect", is_autograph=True)
    with Session(get_engine()) as s:
        is_hit, reason = hit_watchlist.match(c, s)
    assert is_hit is True
    assert reason and "auto" in reason.lower()
