"""B6: Tests for the low_comp_confidence bucket in /api/action-queue."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import get_engine
from app import models
from app.main import app

client = TestClient(app)


def _add_card(**kwargs) -> models.Card:
    defaults = dict(
        player="Test Player",
        year=2021,
        set_brand="Bowman",
        status="Researching",
        comp_confidence=None,
        comp_median=None,
        comp_median_weighted=None,
        est_value_raw=None,
    )
    defaults.update(kwargs)
    card = models.Card(**defaults)
    with Session(get_engine()) as s:
        s.add(card)
        s.commit()
        s.refresh(card)
    return card


class TestLowCompConfidenceBucket:
    def test_bucket_present_in_response(self):
        r = client.get("/api/action-queue")
        assert r.status_code == 200
        assert "low_comp_confidence" in r.json()

    def test_low_confidence_high_value_card_included(self):
        _add_card(comp_confidence="low", comp_median=10.0)
        r = client.get("/api/action-queue")
        bucket = r.json()["low_comp_confidence"]
        assert len(bucket) >= 1

    def test_five_dollar_floor_respected(self):
        # Card worth $4.99 should NOT appear
        _add_card(comp_confidence="low", comp_median=4.99)
        r = client.get("/api/action-queue")
        bucket = r.json()["low_comp_confidence"]
        # All items in bucket must have value >= 5.0
        assert all(item["value"] >= 5.0 for item in bucket)

    def test_exactly_five_dollars_included(self):
        _add_card(comp_confidence="low", comp_median=5.0)
        r = client.get("/api/action-queue")
        bucket = r.json()["low_comp_confidence"]
        assert len(bucket) >= 1

    def test_medium_confidence_not_in_bucket(self):
        _add_card(comp_confidence="medium", comp_median=50.0)
        r = client.get("/api/action-queue")
        bucket = r.json()["low_comp_confidence"]
        # No medium-confidence card should appear
        assert bucket == []

    def test_high_confidence_not_in_bucket(self):
        _add_card(comp_confidence="high", comp_median=100.0)
        r = client.get("/api/action-queue")
        bucket = r.json()["low_comp_confidence"]
        assert bucket == []

    def test_no_confidence_set_not_in_bucket(self):
        _add_card(comp_confidence=None, comp_median=50.0)
        r = client.get("/api/action-queue")
        bucket = r.json()["low_comp_confidence"]
        assert bucket == []

    def test_existing_buckets_still_present(self):
        r = client.get("/api/action-queue")
        data = r.json()
        assert "needs_review" in data
        assert "consider_grading" in data
        assert "needs_photo_verification" in data
        assert "low_comp_confidence" in data

    def test_weighted_median_used_for_value_floor(self):
        """If comp_median_weighted ≥ 5.0 but comp_median < 5.0, card is included."""
        _add_card(comp_confidence="low", comp_median=2.0, comp_median_weighted=6.0)
        r = client.get("/api/action-queue")
        bucket = r.json()["low_comp_confidence"]
        assert len(bucket) >= 1
        assert any(item["value"] == 6.0 for item in bucket)

    def test_bucket_item_schema(self):
        _add_card(comp_confidence="low", comp_median=20.0, player="Nolan Ryan")
        r = client.get("/api/action-queue")
        bucket = r.json()["low_comp_confidence"]
        assert len(bucket) >= 1
        item = bucket[0]
        assert "id" in item
        assert "title" in item
        assert "value" in item
