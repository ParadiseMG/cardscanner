"""B9: Tests for bulk-lot create endpoint."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import models
from app.db import get_engine, session_scope
from app.main import app

client = TestClient(app)


def _seed_cards(n: int = 3, comp_median: float = 0.50) -> list[int]:
    ids = []
    with session_scope() as s:
        for i in range(n):
            c = models.Card(
                player=f"Player {i}",
                year=1989,
                set_brand="Donruss",
                comp_median=comp_median,
            )
            s.add(c)
            s.flush()
            ids.append(c.id)
    return ids


class TestCreateLot:
    def test_create_returns_200(self):
        ids = _seed_cards(3)
        r = client.post("/api/bulk-lots/create", json={
            "card_ids": ids, "label": "1989 Donruss",
            "listing_title": "Lot of 3 1989 Donruss commons",
            "price": 2.99,
        })
        assert r.status_code == 200

    def test_create_returns_bulk_lot_id(self):
        ids = _seed_cards(3)
        r = client.post("/api/bulk-lots/create", json={
            "card_ids": ids, "label": "Test Lot",
        })
        body = r.json()
        assert "bulk_lot_id" in body
        assert isinstance(body["bulk_lot_id"], int)

    def test_create_flips_cards_status_to_bulk(self):
        ids = _seed_cards(3)
        client.post("/api/bulk-lots/create", json={
            "card_ids": ids, "label": "Test Lot",
        })
        with Session(get_engine()) as s:
            for cid in ids:
                card = s.get(models.Card, cid)
                assert card.status == "Bulk"

    def test_create_assigns_lot_id_to_cards(self):
        ids = _seed_cards(3)
        r = client.post("/api/bulk-lots/create", json={
            "card_ids": ids, "label": "Test Lot",
        })
        lot_id = r.json()["bulk_lot_id"]
        with Session(get_engine()) as s:
            for cid in ids:
                card = s.get(models.Card, cid)
                assert card.lot_id == lot_id

    def test_create_linked_cards_count(self):
        ids = _seed_cards(5)
        r = client.post("/api/bulk-lots/create", json={
            "card_ids": ids, "label": "Big Lot",
        })
        assert r.json()["linked_cards"] == 5

    def test_create_persists_bulklot_row(self):
        ids = _seed_cards(2)
        r = client.post("/api/bulk-lots/create", json={
            "card_ids": ids,
            "label": "My Lot",
            "listing_title": "Great Lot of Cards",
            "price": 5.00,
        })
        lot_id = r.json()["bulk_lot_id"]
        with Session(get_engine()) as s:
            lot = s.get(models.BulkLot, lot_id)
        assert lot is not None
        assert lot.label == "My Lot"
        assert lot.listing_title == "Great Lot of Cards"
        assert lot.price == pytest.approx(5.00)
        assert lot.status == "draft"

    def test_create_ignores_missing_card_ids(self):
        ids = _seed_cards(2)
        r = client.post("/api/bulk-lots/create", json={
            "card_ids": ids + [99999], "label": "Test",
        })
        # Should succeed and link only the valid cards
        assert r.status_code == 200
        assert r.json()["linked_cards"] == 2


class TestProposalsEndpoint:
    def test_proposals_returns_200(self):
        r = client.get("/api/bulk-lots/proposals")
        assert r.status_code == 200

    def test_proposals_response_has_proposals_key(self):
        r = client.get("/api/bulk-lots/proposals")
        body = r.json()
        assert "proposals" in body
        assert isinstance(body["proposals"], list)

    def test_proposals_response_has_cached_key(self):
        r = client.get("/api/bulk-lots/proposals")
        body = r.json()
        assert "cached" in body

    def test_proposals_with_seeded_cards(self):
        # Seed sub-$1 cards that should form a lot
        _seed_cards(5, comp_median=0.50)
        # Invalidate cache
        import app.routers.bulk_lots as bl_router
        bl_router._proposals_cache = None
        r = client.get("/api/bulk-lots/proposals")
        body = r.json()
        assert len(body["proposals"]) >= 1

    def test_proposal_fields_present(self):
        _seed_cards(3, comp_median=0.50)
        import app.routers.bulk_lots as bl_router
        bl_router._proposals_cache = None
        r = client.get("/api/bulk-lots/proposals")
        proposals = r.json()["proposals"]
        if proposals:
            p = proposals[0]
            for field in ("card_ids", "cluster_label", "count",
                          "estimated_value", "suggested_title", "suggested_price"):
                assert field in p
