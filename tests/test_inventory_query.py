"""A6: Tests for extended GET /inventory query parameters (A1)."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.main import app
from app import models
from app.db import get_engine

client = TestClient(app)


def _add_card(**kwargs) -> models.Card:
    defaults = dict(
        year=2020, set_brand="Topps", player="Test Player",
        status="Researching", ebay_status="not_listed",
        is_autograph=False, is_relic=False, is_graded=False,
        is_hit_watchlist=False, review_flagged=False,
        comp_median=5.0,
    )
    defaults.update(kwargs)
    with Session(get_engine()) as s:
        c = models.Card(**defaults)
        s.add(c)
        s.commit()
        s.refresh(c)
        return c


# ---------------------------------------------------------------------------
# Soft-delete exclusion
# ---------------------------------------------------------------------------

def test_deleted_excluded_by_default():
    _add_card(player="DeletedGuy", status="Deleted")
    _add_card(player="VisibleGuy", status="Researching")
    r = client.get("/api/inventory")
    assert r.status_code == 200
    players = [i["player"] for i in r.json()["items"]]
    assert "VisibleGuy" in players
    assert "DeletedGuy" not in players


def test_deleted_included_when_opted_in():
    _add_card(player="DeletedGuy", status="Deleted")
    r = client.get("/api/inventory?include_deleted=true")
    players = [i["player"] for i in r.json()["items"]]
    assert "DeletedGuy" in players


# ---------------------------------------------------------------------------
# Sort orders
# ---------------------------------------------------------------------------

def test_sort_value_desc():
    _add_card(player="Cheap", comp_median=1.0)
    _add_card(player="Expensive", comp_median=100.0)
    r = client.get("/api/inventory?sort=value_desc")
    items = r.json()["items"]
    values = [i["comp_median"] for i in items]
    assert values == sorted(values, reverse=True)


def test_sort_value_asc():
    _add_card(player="Cheap", comp_median=1.0)
    _add_card(player="Expensive", comp_median=100.0)
    r = client.get("/api/inventory?sort=value_asc")
    items = r.json()["items"]
    values = [i["comp_median"] for i in items]
    assert values == sorted(values)


def test_sort_year_desc():
    _add_card(player="Old", year=1990)
    _add_card(player="New", year=2022)
    r = client.get("/api/inventory?sort=year_desc")
    items = r.json()["items"]
    years = [i["year"] for i in items]
    assert years == sorted(years, reverse=True)


def test_sort_year_asc():
    _add_card(player="Old", year=1990)
    _add_card(player="New", year=2022)
    r = client.get("/api/inventory?sort=year_asc")
    items = r.json()["items"]
    years = [i["year"] for i in items]
    assert years == sorted(years)


def test_sort_player_az():
    _add_card(player="Zack")
    _add_card(player="Aaron")
    r = client.get("/api/inventory?sort=player_az")
    items = r.json()["items"]
    players = [i["player"] for i in items]
    assert players == sorted(players, key=lambda p: p.lower())


def test_sort_recent():
    _add_card(player="First")
    _add_card(player="Last")
    r = client.get("/api/inventory?sort=recent")
    items = r.json()["items"]
    assert items[0]["player"] == "Last"  # newest first


# ---------------------------------------------------------------------------
# Era filter
# ---------------------------------------------------------------------------

def test_era_filter_single():
    _add_card(player="Modern", year=2005)   # Modern (2000-2014)
    _add_card(player="Vintage", year=1970)  # Vintage (pre-1986)
    r = client.get("/api/inventory?era=Modern+%282000-2014%29")
    items = r.json()["items"]
    assert all(i["era"] == "Modern (2000-2014)" for i in items)


def test_era_filter_multivalued():
    _add_card(player="Modern", year=2005)
    _add_card(player="JunkWax", year=1989)
    _add_card(player="Vintage", year=1970)
    r = client.get("/api/inventory?era=Modern+%282000-2014%29&era=Junk+Wax+%281986-1991%29")
    items = r.json()["items"]
    eras = {i["era"] for i in items}
    assert eras == {"Modern (2000-2014)", "Junk Wax (1986-1991)"}
    players = {i["player"] for i in items}
    assert "Vintage" not in players


# ---------------------------------------------------------------------------
# eBay status filter
# ---------------------------------------------------------------------------

def test_ebay_status_filter():
    _add_card(player="Listed", ebay_status="active")
    _add_card(player="Unlisted", ebay_status="not_listed")
    r = client.get("/api/inventory?ebay_status=active")
    items = r.json()["items"]
    assert all(i["ebay_status"] == "active" for i in items)
    assert any(i["player"] == "Listed" for i in items)


# ---------------------------------------------------------------------------
# Value range filters
# ---------------------------------------------------------------------------

def test_min_value_filter():
    _add_card(player="Cheap", comp_median=1.0)
    _add_card(player="Expensive", comp_median=200.0)
    r = client.get("/api/inventory?min_value=50")
    items = r.json()["items"]
    assert all(i["comp_median"] >= 50 for i in items)
    assert any(i["player"] == "Expensive" for i in items)
    assert all(i["player"] != "Cheap" for i in items)


def test_max_value_filter():
    _add_card(player="Cheap", comp_median=1.0)
    _add_card(player="Expensive", comp_median=200.0)
    r = client.get("/api/inventory?max_value=10")
    items = r.json()["items"]
    assert all((i["comp_median"] or 0) <= 10 for i in items)
    assert any(i["player"] == "Cheap" for i in items)


def test_value_range_combined():
    _add_card(player="InRange", comp_median=25.0)
    _add_card(player="TooLow", comp_median=1.0)
    _add_card(player="TooHigh", comp_median=500.0)
    r = client.get("/api/inventory?min_value=10&max_value=50")
    items = r.json()["items"]
    players = [i["player"] for i in items]
    assert "InRange" in players
    assert "TooLow" not in players
    assert "TooHigh" not in players


# ---------------------------------------------------------------------------
# Boolean attribute filters
# ---------------------------------------------------------------------------

def test_autograph_filter():
    _add_card(player="AutoGuy", is_autograph=True)
    _add_card(player="BaseGuy", is_autograph=False)
    r = client.get("/api/inventory?autograph=true")
    items = r.json()["items"]
    assert all(i["is_autograph"] for i in items)
    players = [i["player"] for i in items]
    assert "AutoGuy" in players
    assert "BaseGuy" not in players


def test_relic_filter():
    _add_card(player="RelicGuy", is_relic=True)
    _add_card(player="NoRelic", is_relic=False)
    r = client.get("/api/inventory?relic=true")
    items = r.json()["items"]
    assert all(i["is_relic"] for i in items)


def test_graded_filter():
    _add_card(player="GradedGuy", is_graded=True, grade="PSA 9")
    _add_card(player="RawGuy", is_graded=False)
    r = client.get("/api/inventory?graded=true")
    items = r.json()["items"]
    assert all(i["is_graded"] for i in items)


# ---------------------------------------------------------------------------
# Composed multi-filter
# ---------------------------------------------------------------------------

def test_multi_filter_composition():
    """Autograph + min_value should return only autographs above threshold."""
    _add_card(player="AutoExpensive", is_autograph=True, comp_median=150.0)
    _add_card(player="AutoCheap", is_autograph=True, comp_median=5.0)
    _add_card(player="BaseExpensive", is_autograph=False, comp_median=150.0)
    r = client.get("/api/inventory?autograph=true&min_value=100")
    items = r.json()["items"]
    players = [i["player"] for i in items]
    assert "AutoExpensive" in players
    assert "AutoCheap" not in players
    assert "BaseExpensive" not in players


def test_total_reflects_filtered_count():
    """total field should count filtered rows, not all rows."""
    _add_card(player="P1", is_autograph=True)
    _add_card(player="P2", is_autograph=False)
    _add_card(player="P3", is_autograph=False)
    r = client.get("/api/inventory?autograph=true")
    data = r.json()
    assert data["total"] == 1
    assert len(data["items"]) == 1
