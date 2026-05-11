"""B9: Tests for GET /api/stats/last_24h."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app import models
from app.db import get_engine, session_scope
from app.main import app

client = TestClient(app)


def _seed_card(
    player: str = "Test Player",
    created_at: datetime = None,
    comp_median: float = 10.0,
    is_hit_watchlist: bool = False,
) -> int:
    if created_at is None:
        created_at = datetime.utcnow()
    with session_scope() as s:
        c = models.Card(
            player=player,
            year=2020,
            comp_median=comp_median,
            is_hit_watchlist=is_hit_watchlist,
            created_at=created_at,
        )
        s.add(c)
        s.flush()
        cid = c.id
    return cid


class TestLast24h:
    def test_returns_200(self):
        r = client.get("/api/stats/last_24h")
        assert r.status_code == 200

    def test_response_has_required_keys(self):
        r = client.get("/api/stats/last_24h")
        body = r.json()
        for key in ("scanned", "hits", "value_added", "ids"):
            assert key in body

    def test_empty_db_returns_zeros(self):
        r = client.get("/api/stats/last_24h")
        body = r.json()
        assert body["scanned"] == 0
        assert body["hits"] == 0
        assert body["value_added"] == pytest.approx(0.0)
        assert body["ids"] == []

    def test_counts_recent_cards(self):
        _seed_card("Recent 1")
        _seed_card("Recent 2")
        r = client.get("/api/stats/last_24h")
        body = r.json()
        assert body["scanned"] == 2

    def test_excludes_old_cards(self):
        # Card created 25 hours ago — should NOT appear
        old_time = datetime.utcnow() - timedelta(hours=25)
        _seed_card("Old Player", created_at=old_time)
        r = client.get("/api/stats/last_24h")
        body = r.json()
        assert body["scanned"] == 0

    def test_includes_23h_old_card(self):
        # Card created 23 hours ago — should appear
        recent_time = datetime.utcnow() - timedelta(hours=23)
        _seed_card("Recent Player", created_at=recent_time)
        r = client.get("/api/stats/last_24h")
        body = r.json()
        assert body["scanned"] == 1

    def test_counts_hits(self):
        _seed_card("Hit Player", is_hit_watchlist=True)
        _seed_card("Non-Hit Player", is_hit_watchlist=False)
        r = client.get("/api/stats/last_24h")
        body = r.json()
        assert body["hits"] == 1

    def test_value_added_sums_comp_medians(self):
        _seed_card("P1", comp_median=15.0)
        _seed_card("P2", comp_median=25.0)
        r = client.get("/api/stats/last_24h")
        body = r.json()
        assert body["value_added"] == pytest.approx(40.0)

    def test_ids_list_contains_recent_cards(self):
        cid1 = _seed_card("Player A")
        cid2 = _seed_card("Player B")
        r = client.get("/api/stats/last_24h")
        body = r.json()
        assert cid1 in body["ids"]
        assert cid2 in body["ids"]

    def test_ids_list_ordered_by_id(self):
        ids = [_seed_card(f"Player {i}") for i in range(5)]
        r = client.get("/api/stats/last_24h")
        body = r.json()
        returned_ids = body["ids"]
        # Should be sorted ascending by id
        assert returned_ids == sorted(returned_ids)

    def test_ids_excludes_old_cards(self):
        old_time = datetime.utcnow() - timedelta(hours=26)
        old_cid = _seed_card("Old Player", created_at=old_time)
        new_cid = _seed_card("New Player")
        r = client.get("/api/stats/last_24h")
        body = r.json()
        assert old_cid not in body["ids"]
        assert new_cid in body["ids"]

    def test_zero_hits_when_no_watchlist_matches(self):
        _seed_card("Player A", is_hit_watchlist=False)
        _seed_card("Player B", is_hit_watchlist=False)
        r = client.get("/api/stats/last_24h")
        body = r.json()
        assert body["hits"] == 0

    def test_mixed_old_and_recent(self):
        old_time = datetime.utcnow() - timedelta(hours=30)
        _seed_card("Old", created_at=old_time, comp_median=100.0)
        new_cid = _seed_card("New", comp_median=20.0)
        r = client.get("/api/stats/last_24h")
        body = r.json()
        assert body["scanned"] == 1
        assert body["value_added"] == pytest.approx(20.0)
        assert body["ids"] == [new_cid]
