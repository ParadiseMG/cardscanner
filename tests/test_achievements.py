"""Achievement engine tests — DB-only, no network."""
from sqlmodel import Session

from app import models, achievements
from app.db import get_engine, session_scope


def test_first_scan_unlocks_after_one_card():
    with session_scope() as s:
        s.add(models.Card(year=2024, set_brand="Topps", player="Test", est_value_raw=2.0))
    with Session(get_engine()) as s:
        new = achievements.evaluate_unlocks(s)
        codes = {a.code for a in new}
    assert "first_scan" in codes


def test_big_hit_triggers_on_value():
    with session_scope() as s:
        s.add(models.Card(year=2018, player="Acuna", comp_median=120.0))
    with Session(get_engine()) as s:
        c = s.exec(__import__("sqlmodel").select(models.Card)).first()
        new = achievements.evaluate_unlocks(s, just_added_card=c)
        codes = {a.code for a in new}
    assert "big_hit" in codes
    assert "first_scan" in codes
    assert "first_100" in codes  # value milestone


def test_unlocks_persist_and_dont_repeat():
    with session_scope() as s:
        s.add(models.Card(player="One", est_value_raw=1.0))
    with Session(get_engine()) as s:
        achievements.evaluate_unlocks(s)
        # second pass should yield nothing new
        again = achievements.evaluate_unlocks(s)
    assert all(a.code != "first_scan" for a in again)


def test_stats_smoke():
    with session_scope() as s:
        s.add(models.Card(year=1989, player="Griffey", set_brand="Upper Deck",
                          card_no="1", is_hit_watchlist=True, comp_median=42.0))
        s.add(models.Card(year=2018, player="Acuna", set_brand="Bowman Chrome",
                          comp_median=80.0, is_autograph=True))
    with Session(get_engine()) as s:
        stats = achievements.compute_stats(s)
    assert stats["total_cards"] == 2
    assert stats["total_value"] == 122.0
    assert stats["hit_count"] == 1
