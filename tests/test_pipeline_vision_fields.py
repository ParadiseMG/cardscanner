"""Tests for A4: pipeline populates new Card fields from CardIdentification.

After _process_one runs, the Card row should have:
- is_rookie, is_serial_numbered, serial_print_run, photo_quality
- low_confidence_fields (JSON-encoded list)
- condition_signals (JSON-encoded dict)
- review_flagged = True when low_confidence_fields is non-empty
- notes contains the auto-flag message when low_confidence_fields non-empty
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image
from sqlmodel import Session, select

from app import models
from app.config import UPLOAD_DIR
from app.db import get_engine
from app.pipeline import _process_one
from app.services import claude_vision, comp_lookup


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _png(path: Path, color=(100, 150, 200)) -> Path:
    Image.new("RGB", (200, 280), color).save(path)
    return path


def _fake_comp(**overrides) -> comp_lookup.CompResult:
    base = dict(
        query="test",
        url="https://example.com",
        prices=[10.0],
        median=10.0,
        low=10.0,
        high=10.0,
        count=1,
        source="none",
        suspicious_bulk=False,
        suspicious_reason="",
        median_recency_weighted=None,
        confidence="low",
    )
    base.update(overrides)
    return comp_lookup.CompResult(**base)


def _fake_ident(**overrides) -> claude_vision.CardIdentification:
    base = dict(
        year=2022,
        set_brand="Topps",
        player="Gunnar Henderson",
        card_no="200",
        parallel="Base",
        condition="NM",
        confidence=0.85,
        is_rookie=False,
        is_serial_numbered=False,
        serial_print_run=None,
        photo_quality="good",
        field_confidence={
            "year": 0.9, "set_brand": 0.9, "player": 0.9,
            "card_no": 0.9, "parallel": 0.9, "condition": 0.8,
        },
        condition_signals={},
        low_confidence_fields=[],
        review_flagged=False,
    )
    base.update(overrides)
    return claude_vision.CardIdentification(**base)


def _run(ident: claude_vision.CardIdentification,
         comp: comp_lookup.CompResult) -> dict:
    p = _png(UPLOAD_DIR / "pvf_test.png")

    async def fake_identify(*a, **kw):
        return ident

    async def fake_fetch(*a, **kw):
        return comp

    with patch.object(claude_vision, "identify_card_async", new=fake_identify), \
         patch.object(comp_lookup, "fetch_comps", new=fake_fetch):
        return asyncio.run(_process_one(p, None))


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_is_rookie_stored():
    result = _run(_fake_ident(is_rookie=True), _fake_comp())
    assert result["ok"] is True
    with Session(get_engine()) as s:
        card = s.get(models.Card, result["card_id"])
        assert card.is_rookie is True


def test_is_not_rookie_default():
    result = _run(_fake_ident(is_rookie=False), _fake_comp())
    with Session(get_engine()) as s:
        card = s.get(models.Card, result["card_id"])
        assert card.is_rookie is False


def test_serial_numbered_fields_stored():
    result = _run(
        _fake_ident(is_serial_numbered=True, serial_print_run=50),
        _fake_comp()
    )
    with Session(get_engine()) as s:
        card = s.get(models.Card, result["card_id"])
        assert card.is_serial_numbered is True
        assert card.serial_print_run == 50


def test_photo_quality_stored():
    result = _run(_fake_ident(photo_quality="blurry"), _fake_comp())
    with Session(get_engine()) as s:
        card = s.get(models.Card, result["card_id"])
        assert card.photo_quality == "blurry"


def test_photo_quality_off_angle():
    result = _run(_fake_ident(photo_quality="off_angle"), _fake_comp())
    with Session(get_engine()) as s:
        card = s.get(models.Card, result["card_id"])
        assert card.photo_quality == "off_angle"


def test_condition_signals_json_encoded():
    signals = {"centering": "60/40", "corners": "sharp", "edges": "clean"}
    result = _run(
        _fake_ident(condition_signals=signals),
        _fake_comp()
    )
    with Session(get_engine()) as s:
        card = s.get(models.Card, result["card_id"])
        assert card.condition_signals is not None
        decoded = json.loads(card.condition_signals)
        assert decoded["centering"] == "60/40"
        assert decoded["corners"] == "sharp"


def test_empty_condition_signals_stored_as_none():
    result = _run(_fake_ident(condition_signals={}), _fake_comp())
    with Session(get_engine()) as s:
        card = s.get(models.Card, result["card_id"])
        assert card.condition_signals is None


def test_low_confidence_fields_json_encoded():
    result = _run(
        _fake_ident(
            low_confidence_fields=["parallel", "card_no"],
            review_flagged=True,
        ),
        _fake_comp()
    )
    with Session(get_engine()) as s:
        card = s.get(models.Card, result["card_id"])
        assert card.low_confidence_fields is not None
        decoded = json.loads(card.low_confidence_fields)
        assert "parallel" in decoded
        assert "card_no" in decoded


def test_empty_low_confidence_fields_stored_as_none():
    result = _run(_fake_ident(low_confidence_fields=[]), _fake_comp())
    with Session(get_engine()) as s:
        card = s.get(models.Card, result["card_id"])
        assert card.low_confidence_fields is None


def test_review_flagged_set_when_low_confidence_fields():
    """When low_confidence_fields is non-empty, review_flagged must be True."""
    result = _run(
        _fake_ident(
            low_confidence_fields=["parallel"],
            review_flagged=False,  # pipeline should override this to True
        ),
        _fake_comp()
    )
    with Session(get_engine()) as s:
        card = s.get(models.Card, result["card_id"])
        assert card.review_flagged is True


def test_notes_contain_auto_flag_message():
    """When low_confidence_fields is non-empty, notes contain the auto-flag text."""
    result = _run(
        _fake_ident(
            low_confidence_fields=["card_no"],
            notes=None,
        ),
        _fake_comp()
    )
    with Session(get_engine()) as s:
        card = s.get(models.Card, result["card_id"])
        assert card.notes is not None
        assert "Auto-flagged" in card.notes
        assert "low confidence" in card.notes


def test_notes_append_to_existing():
    """Auto-flag text is appended to existing notes, not replacing them."""
    result = _run(
        _fake_ident(
            low_confidence_fields=["parallel"],
            notes="Interesting card",
        ),
        _fake_comp()
    )
    with Session(get_engine()) as s:
        card = s.get(models.Card, result["card_id"])
        assert "Interesting card" in card.notes
        assert "Auto-flagged" in card.notes


def test_no_auto_flag_when_no_low_confidence():
    """When low_confidence_fields is empty, notes stay clean."""
    result = _run(
        _fake_ident(
            low_confidence_fields=[],
            notes="Clean card",
            review_flagged=False,
        ),
        _fake_comp()
    )
    with Session(get_engine()) as s:
        card = s.get(models.Card, result["card_id"])
        assert card.notes == "Clean card"
        assert card.review_flagged is False


def test_all_new_fields_none_by_default():
    """When ident has all-default values, new optional fields are None/False."""
    result = _run(_fake_ident(), _fake_comp())
    with Session(get_engine()) as s:
        card = s.get(models.Card, result["card_id"])
        assert card.is_rookie is False
        assert card.is_serial_numbered is False
        assert card.serial_print_run is None
        assert card.photo_quality == "good"  # explicitly set in _fake_ident
        assert card.low_confidence_fields is None
        assert card.condition_signals is None
