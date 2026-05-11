"""Tests for A2: low-confidence re-prompt logic.

Covers:
- Re-prompt fires when field_confidence[field] < 0.5 for 'parallel' or 'card_no'.
- Re-prompt is capped at 1 retry per card (max 2 fields total).
- Merged result has bumped confidence after a successful re-prompt.
- Junk re-prompt response keeps original value (defensive).
- Re-prompt fires for both eligible fields when both are low-confidence.
"""
from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

from app.services import claude_vision
from app.services.claude_vision import (
    CardIdentification,
    identify_card_async,
    _REPROMPT_THRESHOLD,
    _REPROMPT_MAX_FIELDS,
    _REPROMPT_FIELDS,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "content": [{"type": "text", "text": json.dumps(payload)}],
    }
    return resp


def _base_payload(**overrides) -> dict:
    base = {
        "year": 2021,
        "set_brand": "Bowman Chrome",
        "player": "Julio Rodriguez",
        "card_no": "BCP-10",
        "parallel": "Blue Refractor",
        "sport": "Baseball",
        "team": "Mariners",
        "condition": "NM",
        "is_graded": False,
        "grade": None,
        "is_autograph": False,
        "is_relic": False,
        "confidence": 0.75,
        "notes": None,
        "is_rookie": True,
        "is_serial_numbered": False,
        "serial_print_run": None,
        "field_confidence": {
            "year": 0.95,
            "set_brand": 0.90,
            "player": 0.88,
            "card_no": 0.90,
            "parallel": 0.90,
            "condition": 0.75,
        },
        "condition_signals": {},
        "photo_quality": "good",
    }
    base.update(overrides)
    return base


async def _run_identify(payload: dict) -> CardIdentification:
    fake_resp = _make_response(payload)
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=fake_resp)):
        with patch("app.services.claude_vision.normalize_image", side_effect=lambda p: p):
            with patch("app.services.claude_vision._b64_image",
                       return_value={"type": "image", "source": {}}):
                return await identify_card_async("/tmp/front.jpg", api_key_override="sk-test")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_reprompt_fires_for_low_confidence_parallel():
    """When parallel confidence < 0.5, _do_reprompt is called for 'parallel'."""
    payload = _base_payload(
        field_confidence={
            "year": 0.95, "set_brand": 0.90, "player": 0.88,
            "card_no": 0.90,
            "parallel": 0.30,  # below threshold
            "condition": 0.75,
        }
    )
    reprompt_calls = []

    async def fake_reprompt(fp, bp, field_name, current_value, key, client):
        reprompt_calls.append(field_name)
        # Return a high-confidence improved value
        return "Gold Refractor", 0.85

    with patch("app.services.claude_vision._do_reprompt", new=fake_reprompt):
        cid = asyncio.run(_run_identify(payload))

    assert "parallel" in reprompt_calls
    # Value should be updated
    assert cid.parallel == "Gold Refractor"
    # Confidence should be bumped
    assert cid.field_confidence["parallel"] == pytest.approx(0.85)


def test_reprompt_fires_for_low_confidence_card_no():
    """When card_no confidence < 0.5, _do_reprompt is called for 'card_no'."""
    payload = _base_payload(
        field_confidence={
            "year": 0.95, "set_brand": 0.90, "player": 0.88,
            "card_no": 0.20,  # below threshold
            "parallel": 0.90,
            "condition": 0.75,
        }
    )
    reprompt_calls = []

    async def fake_reprompt(fp, bp, field_name, current_value, key, client):
        reprompt_calls.append(field_name)
        return "BCP-15", 0.80

    with patch("app.services.claude_vision._do_reprompt", new=fake_reprompt):
        cid = asyncio.run(_run_identify(payload))

    assert "card_no" in reprompt_calls
    assert cid.card_no == "BCP-15"
    assert cid.field_confidence["card_no"] == pytest.approx(0.80)


def test_reprompt_capped_at_max_fields():
    """When both parallel and card_no are low-confidence, re-prompt fires for both
    but stops at _REPROMPT_MAX_FIELDS (2)."""
    payload = _base_payload(
        field_confidence={
            "year": 0.95, "set_brand": 0.90, "player": 0.88,
            "card_no": 0.10,   # low
            "parallel": 0.15,  # low
            "condition": 0.75,
        }
    )
    reprompt_calls = []

    async def fake_reprompt(fp, bp, field_name, current_value, key, client):
        reprompt_calls.append(field_name)
        return current_value, 0.80

    with patch("app.services.claude_vision._do_reprompt", new=fake_reprompt):
        cid = asyncio.run(_run_identify(payload))

    # Both fields are eligible; max is 2, so exactly 2 calls
    assert len(reprompt_calls) == _REPROMPT_MAX_FIELDS
    assert set(reprompt_calls) == set(_REPROMPT_FIELDS)


def test_no_reprompt_when_confidence_above_threshold():
    """No re-prompt when all fields are >= 0.5."""
    payload = _base_payload(
        field_confidence={
            "year": 0.95, "set_brand": 0.90, "player": 0.88,
            "card_no": 0.80,
            "parallel": 0.75,
            "condition": 0.75,
        }
    )
    reprompt_calls = []

    async def fake_reprompt(fp, bp, field_name, current_value, key, client):
        reprompt_calls.append(field_name)
        return current_value, 0.90

    with patch("app.services.claude_vision._do_reprompt", new=fake_reprompt):
        cid = asyncio.run(_run_identify(payload))

    assert reprompt_calls == []


def test_reprompt_bumped_confidence_after_successful_reprompt():
    """After a successful re-prompt, confidence is bumped in field_confidence."""
    payload = _base_payload(
        parallel="Base",
        field_confidence={
            "year": 0.9, "set_brand": 0.9, "player": 0.9,
            "card_no": 0.9,
            "parallel": 0.25,  # low
            "condition": 0.8,
        }
    )

    async def fake_reprompt(fp, bp, field_name, current_value, key, client):
        return "Gold /50", 0.92

    with patch("app.services.claude_vision._do_reprompt", new=fake_reprompt):
        cid = asyncio.run(_run_identify(payload))

    assert cid.field_confidence["parallel"] == pytest.approx(0.92)
    assert cid.parallel == "Gold /50"
    # Since confidence went up to 0.92, it should no longer be in low_confidence_fields
    assert "parallel" not in cid.low_confidence_fields


def test_junk_reprompt_keeps_original_value():
    """When _do_reprompt returns junk (error path), original value is preserved."""
    payload = _base_payload(
        parallel="Refractor",
        field_confidence={
            "year": 0.9, "set_brand": 0.9, "player": 0.9,
            "card_no": 0.9,
            "parallel": 0.30,  # low — will trigger reprompt
            "condition": 0.8,
        }
    )

    # Simulate junk: returns original value with 0.0 confidence
    async def fake_reprompt(fp, bp, field_name, current_value, key, client):
        return current_value, 0.0  # junk path — no change

    with patch("app.services.claude_vision._do_reprompt", new=fake_reprompt):
        cid = asyncio.run(_run_identify(payload))

    # Original value must be preserved
    assert cid.parallel == "Refractor"
    # Field still in low_confidence_fields because conf stayed 0.0 < 0.5
    assert "parallel" in cid.low_confidence_fields


def test_reprompt_logs_step(caplog):
    """Re-prompt path emits a log step entry."""
    import logging
    payload = _base_payload(
        field_confidence={
            "year": 0.9, "set_brand": 0.9, "player": 0.9,
            "card_no": 0.9,
            "parallel": 0.20,  # low
            "condition": 0.8,
        }
    )

    async def fake_reprompt(fp, bp, field_name, current_value, key, client):
        return current_value, 0.80

    with caplog.at_level(logging.INFO, logger="app.services.claude_vision"):
        with patch("app.services.claude_vision._do_reprompt", new=fake_reprompt):
            cid = asyncio.run(_run_identify(payload))

    # Should have logged the vision_reprompt step
    assert any("vision_reprompt" in r.message for r in caplog.records)


def test_reprompt_end_to_end_low_then_high():
    """Full end-to-end: first call returns low confidence on parallel,
    re-prompt returns high confidence. parallel field updated."""
    low_conf_payload = _base_payload(
        parallel="Base",
        field_confidence={
            "year": 0.9, "set_brand": 0.9, "player": 0.9,
            "card_no": 0.9,
            "parallel": 0.20,  # triggers re-prompt
            "condition": 0.8,
        }
    )

    async def good_reprompt(fp, bp, field_name, current_value, key, client):
        assert field_name == "parallel"
        return "Gold Refractor /50", 0.95

    with patch("app.services.claude_vision._do_reprompt", new=good_reprompt):
        cid = asyncio.run(_run_identify(low_conf_payload))

    assert cid.parallel == "Gold Refractor /50"
    assert cid.field_confidence["parallel"] == pytest.approx(0.95)
    assert "parallel" not in cid.low_confidence_fields
