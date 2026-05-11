"""Tests for A1: enriched CardIdentification parser.

Covers:
- Enriched JSON response shape is parsed correctly.
- Missing new fields default safely.
- Markdown fence stripping still works.
- _parse_json_block tolerates extra whitespace / fence variants.
"""
from __future__ import annotations

import json
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services import claude_vision
from app.services.claude_vision import (
    CardIdentification,
    _parse_json_block,
    identify_card_async,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_response(payload: dict) -> MagicMock:
    """Build a fake httpx response that returns `payload` as JSON."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "content": [{"type": "text", "text": json.dumps(payload)}],
    }
    return resp


def _full_payload(**overrides) -> dict:
    """Minimal valid enriched payload."""
    base = {
        "year": 2021,
        "set_brand": "Topps Chrome",
        "player": "Fernando Tatis Jr",
        "card_no": "200",
        "parallel": "Refractor",
        "sport": "Baseball",
        "team": "Padres",
        "condition": "NM",
        "is_graded": False,
        "grade": None,
        "is_autograph": False,
        "is_relic": False,
        "confidence": 0.92,
        "notes": None,
        "is_rookie": False,
        "is_serial_numbered": True,
        "serial_print_run": 99,
        "field_confidence": {
            "year": 0.95,
            "set_brand": 0.90,
            "player": 0.95,
            "card_no": 0.80,
            "parallel": 0.85,
            "condition": 0.70,
        },
        "condition_signals": {
            "centering": "50/50",
            "corners": "sharp",
            "edges": "clean",
            "surface": "clean",
        },
        "photo_quality": "good",
    }
    base.update(overrides)
    return base


async def _run_identify(payload: dict, front_path: str = "/tmp/front.jpg") -> CardIdentification:
    """Run identify_card_async with a mocked HTTP client."""
    fake_resp = _make_response(payload)
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=fake_resp)):
        # Also patch normalize_image so we don't need a real file
        with patch("app.services.claude_vision.normalize_image", side_effect=lambda p: p):
            # Patch _b64_image so we don't read the filesystem
            with patch("app.services.claude_vision._b64_image", return_value={"type": "image", "source": {}}):
                return await identify_card_async(front_path, api_key_override="sk-test")


# ---------------------------------------------------------------------------
# Tests: enriched response shape
# ---------------------------------------------------------------------------

def test_enriched_fields_parsed():
    payload = _full_payload()
    cid = asyncio.run(_run_identify(payload))

    assert cid.is_serial_numbered is True
    assert cid.serial_print_run == 99
    assert cid.photo_quality == "good"
    assert cid.field_confidence["year"] == pytest.approx(0.95)
    assert cid.field_confidence["parallel"] == pytest.approx(0.85)
    assert cid.condition_signals["centering"] == "50/50"
    assert cid.condition_signals["corners"] == "sharp"


def test_is_rookie_true():
    cid = asyncio.run(_run_identify(_full_payload(is_rookie=True)))
    assert cid.is_rookie is True


def test_serial_numbered_with_print_run():
    cid = asyncio.run(_run_identify(_full_payload(
        is_serial_numbered=True, serial_print_run=50
    )))
    assert cid.is_serial_numbered is True
    assert cid.serial_print_run == 50


def test_photo_quality_blurry():
    cid = asyncio.run(_run_identify(_full_payload(photo_quality="blurry")))
    assert cid.photo_quality == "blurry"


def test_high_confidence_no_low_confidence_fields():
    """When all field_confidence values are >= 0.5, low_confidence_fields is empty."""
    cid = asyncio.run(_run_identify(_full_payload()))
    # All field_confidence in _full_payload are >= 0.5
    assert cid.low_confidence_fields == []


def test_low_confidence_fields_populated():
    """Fields with confidence < 0.5 appear in low_confidence_fields."""
    payload = _full_payload(
        field_confidence={
            "year": 0.9,
            "set_brand": 0.9,
            "player": 0.9,
            "card_no": 0.3,   # low
            "parallel": 0.2,  # low
            "condition": 0.8,
        }
    )
    # Both card_no and parallel are low-confidence — re-prompts will fire.
    # Mock _do_reprompt to return a slightly better value so we can test the
    # low_confidence_fields computation after reprompts fail to cross threshold.
    async def fake_reprompt(fp, bp, field_name, current_value, key, client):
        # Return same value, same low conf — fields remain low
        return current_value, 0.25

    with patch("app.services.claude_vision._do_reprompt", new=fake_reprompt):
        cid = asyncio.run(_run_identify(payload))

    # Both should still be low confidence after re-prompt returning 0.25
    assert "card_no" in cid.low_confidence_fields
    assert "parallel" in cid.low_confidence_fields


# ---------------------------------------------------------------------------
# Tests: missing-field defaults
# ---------------------------------------------------------------------------

def test_missing_new_fields_default_safely():
    """A response without the new fields doesn't crash; defaults apply."""
    payload = {
        "year": 2019,
        "set_brand": "Topps",
        "player": "Pete Alonso",
        "card_no": "475",
        "parallel": "Base",
        "sport": "Baseball",
        "team": "Mets",
        "condition": "NM",
        "is_graded": False,
        "grade": None,
        "is_autograph": False,
        "is_relic": False,
        "confidence": 0.88,
        "notes": None,
        # No is_rookie, is_serial_numbered, serial_print_run,
        # field_confidence, condition_signals, photo_quality
    }
    cid = asyncio.run(_run_identify(payload))

    assert cid.is_rookie is False
    assert cid.is_serial_numbered is False
    assert cid.serial_print_run is None
    assert cid.photo_quality is None
    assert cid.field_confidence == {}
    assert cid.condition_signals == {}
    assert cid.low_confidence_fields == []


def test_null_condition_signals_skipped():
    """Null values in condition_signals are omitted from the dict."""
    payload = _full_payload(condition_signals={
        "centering": "60/40",
        "corners": None,   # null → omitted
        "edges": "clean",
        "surface": None,   # null → omitted
    })
    cid = asyncio.run(_run_identify(payload))
    assert "centering" in cid.condition_signals
    assert "edges" in cid.condition_signals
    assert "corners" not in cid.condition_signals
    assert "surface" not in cid.condition_signals


def test_malformed_field_confidence_defaults_zero():
    """Non-numeric field_confidence values default to 0.0."""
    payload = _full_payload(field_confidence={
        "year": "high",  # bad value
        "player": 0.9,
    })
    cid = asyncio.run(_run_identify(payload))
    assert cid.field_confidence["year"] == pytest.approx(0.0)
    assert cid.field_confidence["player"] == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# Tests: _parse_json_block / markdown fence stripping
# ---------------------------------------------------------------------------

def test_parse_plain_json():
    raw = '{"year": 2020, "player": "Mike Trout"}'
    result = _parse_json_block(raw)
    assert result["year"] == 2020
    assert result["player"] == "Mike Trout"


def test_parse_json_with_markdown_fences():
    raw = '```json\n{"year": 2020, "player": "Mike Trout"}\n```'
    result = _parse_json_block(raw)
    assert result["year"] == 2020


def test_parse_json_with_plain_code_fence():
    raw = '```\n{"year": 2018}\n```'
    result = _parse_json_block(raw)
    assert result["year"] == 2018


def test_parse_json_with_surrounding_prose():
    raw = 'Here is the JSON:\n{"year": 2015, "player": "Bryce Harper"}\nEnd.'
    result = _parse_json_block(raw)
    assert result["year"] == 2015


def test_parse_json_raises_on_no_object():
    with pytest.raises(ValueError, match="no JSON object"):
        _parse_json_block("This is not JSON at all!")


def test_parse_json_whitespace_only():
    with pytest.raises(ValueError):
        _parse_json_block("   \n  ")
