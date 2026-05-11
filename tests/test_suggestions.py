"""B6: Tests for GET /api/suggestions endpoint."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import get_engine
from app import models
from app.main import app


client = TestClient(app)


def _add_card(**kwargs) -> models.Card:
    """Create and persist a Card with defaults suitable for testing."""
    defaults = dict(
        player="Test Player",
        year=2020,
        set_brand="Topps",
        status="Researching",
        ebay_status="not_listed",
        is_graded=False,
        is_hit_watchlist=False,
        comp_median=None,
        comp_median_weighted=None,
        est_value_raw=None,
        comp_suspicious_bulk=False,
        comp_suspicious_reason=None,
    )
    defaults.update(kwargs)
    card = models.Card(**defaults)
    with Session(get_engine()) as s:
        s.add(card)
        s.commit()
        s.refresh(card)
    return card


# ---------------------------------------------------------------------------
# grade suggestions
# ---------------------------------------------------------------------------
class TestGradeSuggestions:
    def test_high_value_raw_card_generates_grade(self):
        _add_card(comp_median=100.0, is_graded=False)
        r = client.get("/api/suggestions")
        assert r.status_code == 200
        kinds = [i["kind"] for i in r.json()["items"]]
        assert "grade" in kinds

    def test_already_graded_excluded(self):
        _add_card(comp_median=150.0, is_graded=True)
        r = client.get("/api/suggestions")
        grade_items = [i for i in r.json()["items"] if i["kind"] == "grade"]
        assert all(i["value"] >= 80 for i in grade_items)

    def test_below_80_excluded_from_grade(self):
        _add_card(comp_median=79.0, is_graded=False)
        r = client.get("/api/suggestions")
        # Only this card exists — no grade suggestions should appear
        grade_items = [i for i in r.json()["items"] if i["kind"] == "grade"]
        assert grade_items == []

    def test_deleted_excluded_from_grade(self):
        _add_card(comp_median=200.0, is_graded=False, status="Deleted")
        r = client.get("/api/suggestions")
        grade_items = [i for i in r.json()["items"] if i["kind"] == "grade"]
        assert grade_items == []


# ---------------------------------------------------------------------------
# bulk suggestions
# ---------------------------------------------------------------------------
class TestBulkSuggestions:
    def test_sub_dollar_card_generates_bulk(self):
        _add_card(comp_median=0.50, status="Researching")
        r = client.get("/api/suggestions")
        kinds = [i["kind"] for i in r.json()["items"]]
        assert "bulk" in kinds

    def test_already_bulk_status_excluded(self):
        _add_card(comp_median=0.25, status="Bulk")
        r = client.get("/api/suggestions")
        bulk_items = [i for i in r.json()["items"] if i["kind"] == "bulk"]
        assert bulk_items == []

    def test_sold_excluded_from_bulk(self):
        _add_card(comp_median=0.25, status="Sold")
        r = client.get("/api/suggestions")
        bulk_items = [i for i in r.json()["items"] if i["kind"] == "bulk"]
        assert bulk_items == []

    def test_deleted_excluded_from_bulk(self):
        _add_card(comp_median=0.25, status="Deleted")
        r = client.get("/api/suggestions")
        bulk_items = [i for i in r.json()["items"] if i["kind"] == "bulk"]
        assert bulk_items == []

    def test_one_dollar_not_bulk(self):
        _add_card(comp_median=1.0, status="Researching")
        r = client.get("/api/suggestions")
        # 1.0 is NOT < 1.0, so it shouldn't generate a bulk suggestion
        bulk_items = [i for i in r.json()["items"] if i["kind"] == "bulk"]
        assert bulk_items == []


# ---------------------------------------------------------------------------
# list suggestions
# ---------------------------------------------------------------------------
class TestListSuggestions:
    def test_hit_watchlist_not_listed_generates_list(self):
        _add_card(is_hit_watchlist=True, ebay_status="not_listed", comp_median=50.0)
        r = client.get("/api/suggestions")
        kinds = [i["kind"] for i in r.json()["items"]]
        assert "list" in kinds

    def test_already_listed_excluded(self):
        _add_card(is_hit_watchlist=True, ebay_status="active", comp_median=50.0)
        r = client.get("/api/suggestions")
        list_items = [i for i in r.json()["items"] if i["kind"] == "list"]
        assert list_items == []

    def test_not_hit_watchlist_excluded(self):
        _add_card(is_hit_watchlist=False, ebay_status="not_listed", comp_median=50.0)
        r = client.get("/api/suggestions")
        list_items = [i for i in r.json()["items"] if i["kind"] == "list"]
        assert list_items == []

    def test_sold_status_excluded(self):
        _add_card(is_hit_watchlist=True, ebay_status="not_listed", status="Sold", comp_median=50.0)
        r = client.get("/api/suggestions")
        list_items = [i for i in r.json()["items"] if i["kind"] == "list"]
        assert list_items == []

    def test_deleted_excluded_from_list(self):
        _add_card(is_hit_watchlist=True, ebay_status="not_listed", status="Deleted", comp_median=50.0)
        r = client.get("/api/suggestions")
        list_items = [i for i in r.json()["items"] if i["kind"] == "list"]
        assert list_items == []


# ---------------------------------------------------------------------------
# reshoot suggestions
# ---------------------------------------------------------------------------
class TestReshootSuggestions:
    def test_blurry_generates_reshoot(self):
        _add_card(photo_quality="blurry", comp_median=10.0)
        r = client.get("/api/suggestions")
        kinds = [i["kind"] for i in r.json()["items"]]
        assert "reshoot" in kinds

    def test_obstructed_generates_reshoot(self):
        _add_card(photo_quality="obstructed", comp_median=10.0)
        r = client.get("/api/suggestions")
        kinds = [i["kind"] for i in r.json()["items"]]
        assert "reshoot" in kinds

    def test_off_angle_generates_reshoot(self):
        _add_card(photo_quality="off_angle", comp_median=10.0)
        r = client.get("/api/suggestions")
        kinds = [i["kind"] for i in r.json()["items"]]
        assert "reshoot" in kinds

    def test_good_quality_excluded(self):
        _add_card(photo_quality="good", comp_median=10.0)
        r = client.get("/api/suggestions")
        reshoot_items = [i for i in r.json()["items"] if i["kind"] == "reshoot"]
        assert reshoot_items == []

    def test_no_photo_quality_excluded(self):
        _add_card(photo_quality=None, comp_median=10.0)
        r = client.get("/api/suggestions")
        reshoot_items = [i for i in r.json()["items"] if i["kind"] == "reshoot"]
        assert reshoot_items == []

    def test_deleted_excluded_from_reshoot(self):
        _add_card(photo_quality="blurry", status="Deleted", comp_median=10.0)
        r = client.get("/api/suggestions")
        reshoot_items = [i for i in r.json()["items"] if i["kind"] == "reshoot"]
        assert reshoot_items == []


# ---------------------------------------------------------------------------
# verify_comps suggestions
# ---------------------------------------------------------------------------
class TestVerifyCompsSuggestions:
    def test_suspicious_bulk_generates_verify_comps(self):
        _add_card(
            comp_suspicious_bulk=True,
            comp_suspicious_reason="5 sales at $10.00 — possible bulk lot fingerprint",
            comp_median=10.0,
        )
        r = client.get("/api/suggestions")
        kinds = [i["kind"] for i in r.json()["items"]]
        assert "verify_comps" in kinds

    def test_not_suspicious_excluded(self):
        _add_card(comp_suspicious_bulk=False, comp_median=10.0)
        r = client.get("/api/suggestions")
        vc_items = [i for i in r.json()["items"] if i["kind"] == "verify_comps"]
        assert vc_items == []

    def test_suspicious_reason_in_response(self):
        reason = "7 sales at $5.00 — possible bulk lot fingerprint"
        _add_card(comp_suspicious_bulk=True, comp_suspicious_reason=reason, comp_median=5.0)
        r = client.get("/api/suggestions")
        vc_items = [i for i in r.json()["items"] if i["kind"] == "verify_comps"]
        assert len(vc_items) >= 1
        assert vc_items[0]["reason"] == reason

    def test_deleted_excluded_from_verify_comps(self):
        _add_card(comp_suspicious_bulk=True, status="Deleted", comp_median=10.0)
        r = client.get("/api/suggestions")
        vc_items = [i for i in r.json()["items"] if i["kind"] == "verify_comps"]
        assert vc_items == []


# ---------------------------------------------------------------------------
# Cap at 100 and ordering
# ---------------------------------------------------------------------------
class TestCapAndOrdering:
    def test_capped_at_100(self):
        # Insert 110 sub-dollar cards
        with Session(get_engine()) as s:
            for i in range(110):
                s.add(models.Card(
                    player=f"Player {i}",
                    comp_median=0.25,
                    status="Researching",
                    ebay_status="not_listed",
                    is_graded=False,
                    is_hit_watchlist=False,
                    comp_suspicious_bulk=False,
                ))
            s.commit()
        r = client.get("/api/suggestions")
        assert r.status_code == 200
        assert len(r.json()["items"]) <= 100

    def test_sorted_by_value_descending(self):
        # Add cards with known values
        _add_card(comp_median=50.0, is_hit_watchlist=True, ebay_status="not_listed")
        _add_card(comp_median=30.0, is_hit_watchlist=True, ebay_status="not_listed")
        _add_card(comp_median=80.0, is_hit_watchlist=True, ebay_status="not_listed")
        r = client.get("/api/suggestions")
        items = r.json()["items"]
        values = [i["value"] for i in items]
        assert values == sorted(values, reverse=True)

    def test_response_schema_fields(self):
        _add_card(comp_median=0.50, status="Researching")
        r = client.get("/api/suggestions")
        assert r.status_code == 200
        data = r.json()
        assert "items" in data
        if data["items"]:
            item = data["items"][0]
            for field in ("id", "kind", "title", "value", "reason"):
                assert field in item
