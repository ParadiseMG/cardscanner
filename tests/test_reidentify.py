"""B9: Tests for POST /api/inventory/{id}/reidentify.

Mocks Claude so no real API calls are made.
"""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from app import models
from app.db import get_engine, session_scope
from app.main import app
from app.services import claude_vision
from app.config import UPLOAD_DIR

client = TestClient(app)


def _seed_card(
    player: str = "Original Player",
    year: int = 2010,
    set_brand: str = "Topps",
    front_image: str = "front_test.jpg",
    user_overrides: list[str] = None,
) -> int:
    # Create a dummy image file so the path exists
    img_path = UPLOAD_DIR / front_image
    if not img_path.exists():
        img_path.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)  # minimal JPEG header

    with session_scope() as s:
        c = models.Card(
            player=player,
            year=year,
            set_brand=set_brand,
            front_image=front_image,
            user_overrides=json.dumps(user_overrides) if user_overrides else None,
        )
        s.add(c)
        s.flush()
        cid = c.id
    return cid


def _make_fake_ident(**kwargs) -> claude_vision.CardIdentification:
    defaults = dict(
        year=2019,
        player="Different Player",
        set_brand="Bowman",
        card_no="100",
        parallel="Base",
        sport="Baseball",
        team="Yankees",
        condition="NM",
        is_graded=False,
        grade=None,
        is_autograph=False,
        is_relic=False,
        confidence=0.95,
        notes=None,
        is_rookie=False,
        is_serial_numbered=False,
        serial_print_run=None,
        field_confidence={"year": 0.9, "player": 0.9},
        condition_signals={},
        photo_quality="good",
    )
    defaults.update(kwargs)
    return claude_vision.CardIdentification(**defaults)


class TestReidentifyEndpoint:
    def test_returns_200_with_mocked_claude(self):
        cid = _seed_card()

        async def fake_ident(*a, **kw):
            return _make_fake_ident()

        with patch.object(claude_vision, "identify_card_async", new=fake_ident):
            r = client.post(f"/api/inventory/{cid}/reidentify",
                            headers={"X-Anthropic-Key": "sk-test"})
        assert r.status_code == 200

    def test_merges_player_from_claude(self):
        cid = _seed_card(player="Original Player")

        async def fake_ident(*a, **kw):
            return _make_fake_ident(player="New Player From Claude")

        with patch.object(claude_vision, "identify_card_async", new=fake_ident):
            r = client.post(f"/api/inventory/{cid}/reidentify",
                            headers={"X-Anthropic-Key": "sk-test"})

        assert r.json()["player"] == "New Player From Claude"

    def test_merges_year_from_claude(self):
        cid = _seed_card(year=2010)

        async def fake_ident(*a, **kw):
            return _make_fake_ident(year=2019)

        with patch.object(claude_vision, "identify_card_async", new=fake_ident):
            r = client.post(f"/api/inventory/{cid}/reidentify",
                            headers={"X-Anthropic-Key": "sk-test"})

        assert r.json()["year"] == 2019

    def test_respects_player_override(self):
        """When player is in user_overrides, it must NOT be overwritten."""
        cid = _seed_card(player="Locked Player", user_overrides=["player"])

        async def fake_ident(*a, **kw):
            return _make_fake_ident(player="Different Player From Claude")

        with patch.object(claude_vision, "identify_card_async", new=fake_ident):
            r = client.post(f"/api/inventory/{cid}/reidentify",
                            headers={"X-Anthropic-Key": "sk-test"})

        # player should remain "Locked Player", NOT "Different Player From Claude"
        assert r.json()["player"] == "Locked Player"

    def test_respects_year_override(self):
        cid = _seed_card(year=1989, user_overrides=["year"])

        async def fake_ident(*a, **kw):
            return _make_fake_ident(year=2019)

        with patch.object(claude_vision, "identify_card_async", new=fake_ident):
            r = client.post(f"/api/inventory/{cid}/reidentify",
                            headers={"X-Anthropic-Key": "sk-test"})

        assert r.json()["year"] == 1989

    def test_non_overridden_fields_are_updated(self):
        """user_overrides=["player"] — set_brand should still be updated."""
        cid = _seed_card(player="Locked", set_brand="Old Brand", user_overrides=["player"])

        async def fake_ident(*a, **kw):
            return _make_fake_ident(player="New Claude Player", set_brand="New Brand")

        with patch.object(claude_vision, "identify_card_async", new=fake_ident):
            r = client.post(f"/api/inventory/{cid}/reidentify",
                            headers={"X-Anthropic-Key": "sk-test"})

        body = r.json()
        assert body["player"] == "Locked"
        assert body["set_brand"] == "New Brand"

    def test_returns_card_out_fields(self):
        cid = _seed_card()

        async def fake_ident(*a, **kw):
            return _make_fake_ident()

        with patch.object(claude_vision, "identify_card_async", new=fake_ident):
            r = client.post(f"/api/inventory/{cid}/reidentify",
                            headers={"X-Anthropic-Key": "sk-test"})

        body = r.json()
        for field in ("id", "player", "year", "set_brand", "status"):
            assert field in body

    def test_404_for_missing_card(self):
        async def fake_ident(*a, **kw):
            return _make_fake_ident()

        with patch.object(claude_vision, "identify_card_async", new=fake_ident):
            r = client.post("/api/inventory/99999/reidentify",
                            headers={"X-Anthropic-Key": "sk-test"})
        assert r.status_code == 404

    def test_400_when_no_front_image(self):
        with session_scope() as s:
            c = models.Card(player="No Image", year=2020)
            s.add(c)
            s.flush()
            cid = c.id

        async def fake_ident(*a, **kw):
            return _make_fake_ident()

        with patch.object(claude_vision, "identify_card_async", new=fake_ident):
            r = client.post(f"/api/inventory/{cid}/reidentify",
                            headers={"X-Anthropic-Key": "sk-test"})
        assert r.status_code == 400

    def test_multiple_overrides(self):
        cid = _seed_card(player="P", year=1989, set_brand="Topps",
                         user_overrides=["player", "year"])

        async def fake_ident(*a, **kw):
            return _make_fake_ident(player="New", year=2020, set_brand="Bowman")

        with patch.object(claude_vision, "identify_card_async", new=fake_ident):
            r = client.post(f"/api/inventory/{cid}/reidentify",
                            headers={"X-Anthropic-Key": "sk-test"})

        body = r.json()
        assert body["player"] == "P"
        assert body["year"] == 1989
        assert body["set_brand"] == "Bowman"  # NOT overridden
