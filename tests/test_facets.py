"""A6: Tests for GET /inventory/facets (A2)."""
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
        year=2020, set_brand="Topps", player="Facet Player",
        status="Researching", ebay_status="not_listed",
        is_autograph=False, is_relic=False, is_graded=False,
        is_hit_watchlist=False, review_flagged=False,
        comp_median=None, est_value_raw=None,
    )
    defaults.update(kwargs)
    with Session(get_engine()) as s:
        c = models.Card(**defaults)
        s.add(c)
        s.commit()
        s.refresh(c)
        return c


def test_facets_structure():
    """Response should have the four required top-level keys."""
    r = client.get("/api/inventory/facets")
    assert r.status_code == 200
    data = r.json()
    assert "eras" in data
    assert "ebay_status" in data
    assert "value_buckets" in data
    assert "tags" in data


def test_facets_era_counts():
    _add_card(player="JunkWax1", year=1988)  # Junk Wax (1986-1991)
    _add_card(player="JunkWax2", year=1990)
    _add_card(player="Modern", year=2005)    # Modern (2000-2014)
    r = client.get("/api/inventory/facets")
    data = r.json()
    assert data["eras"].get("Junk Wax (1986-1991)", 0) == 2
    assert data["eras"].get("Modern (2000-2014)", 0) == 1


def test_facets_ebay_status_counts():
    _add_card(ebay_status="not_listed")
    _add_card(ebay_status="not_listed")
    _add_card(ebay_status="active")
    r = client.get("/api/inventory/facets")
    data = r.json()
    es = data["ebay_status"]
    assert es.get("not_listed", 0) == 2
    assert es.get("active", 0) == 1


def test_facets_value_buckets():
    _add_card(comp_median=0.5)   # 0-1
    _add_card(comp_median=5.0)   # 1-10
    _add_card(comp_median=25.0)  # 10-50
    _add_card(comp_median=100.0) # 50-200
    _add_card(comp_median=300.0) # 200+
    r = client.get("/api/inventory/facets")
    data = r.json()
    vb = data["value_buckets"]
    assert vb["0-1"] >= 1
    assert vb["1-10"] >= 1
    assert vb["10-50"] >= 1
    assert vb["50-200"] >= 1
    assert vb["200+"] >= 1


def test_facets_value_bucket_no_comp_falls_to_zero():
    _add_card(comp_median=None, est_value_raw=None)
    r = client.get("/api/inventory/facets")
    data = r.json()
    assert data["value_buckets"]["0-1"] >= 1  # 0 value → 0-1 bucket


def test_facets_tags_counts():
    _add_card(is_autograph=True)
    _add_card(is_autograph=True)
    _add_card(is_relic=True)
    _add_card(is_graded=True)
    _add_card(is_hit_watchlist=True)
    _add_card()  # no tags
    r = client.get("/api/inventory/facets")
    data = r.json()
    tags = data["tags"]
    assert tags["autograph"] == 2
    assert tags["relic"] == 1
    assert tags["graded"] == 1
    assert tags["hit"] == 1


def test_facets_excludes_deleted():
    _add_card(player="Deleted", status="Deleted", is_autograph=True)
    _add_card(player="Visible", status="Researching", is_autograph=True)
    r = client.get("/api/inventory/facets")
    data = r.json()
    # Only one autograph visible
    assert data["tags"]["autograph"] == 1


def test_facets_all_value_bucket_keys_present():
    r = client.get("/api/inventory/facets")
    vb = r.json()["value_buckets"]
    for key in ("0-1", "1-10", "10-50", "50-200", "200+"):
        assert key in vb, f"Missing bucket key: {key}"


def test_facets_empty_db_returns_zeros():
    """With no cards, all counts should be zero or absent."""
    r = client.get("/api/inventory/facets")
    assert r.status_code == 200
    data = r.json()
    # All tag counts should be 0
    for v in data["tags"].values():
        assert v == 0
    # All value buckets should be 0
    for v in data["value_buckets"].values():
        assert v == 0
