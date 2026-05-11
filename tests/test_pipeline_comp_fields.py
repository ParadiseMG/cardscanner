"""B6: After _process_one, the new comp fields are populated on the Card row.
Also verifies that suspicious-bulk triggers the note-append.
"""
from __future__ import annotations

import asyncio
import io
from pathlib import Path
from unittest.mock import patch

import pytest
from PIL import Image
from sqlmodel import Session, select

from app import models
from app.db import get_engine
from app.pipeline import _process_one
from app.services import claude_vision, comp_lookup
from app.config import UPLOAD_DIR


def _png(path: Path, color=(0, 128, 255)) -> Path:
    Image.new("RGB", (200, 280), color).save(path)
    return path


def _fake_ident(**kwargs) -> claude_vision.CardIdentification:
    defaults = dict(
        year=2019, set_brand="Topps Chrome", player="Pipeline Tester",
        card_no="100", condition="NM", confidence=0.95,
    )
    defaults.update(kwargs)
    return claude_vision.CardIdentification(**defaults)


def _fake_comp(**kwargs) -> comp_lookup.CompResult:
    defaults = dict(
        query="test", url="http://example.com",
        prices=[10.0] * 3,
        median=10.0, low=10.0, high=10.0, count=3,
        confidence="low",
        suspicious_bulk=False,
        suspicious_reason="",
        median_recency_weighted=None,
    )
    defaults.update(kwargs)
    return comp_lookup.CompResult(**defaults)


class TestPipelineCompFields:
    def test_comp_confidence_populated(self):
        p = _png(UPLOAD_DIR / "pcf_conf_test.png", color=(10, 20, 30))
        ident = _fake_ident()
        comp = _fake_comp(confidence="medium", median=25.0)

        async def fid(*a, **kw): return ident
        async def fcomp(*a, **kw): return comp

        with patch.object(claude_vision, "identify_card_async", new=fid), \
             patch.object(comp_lookup, "fetch_comps", new=fcomp):
            result = asyncio.run(_process_one(p, None))

        assert result["ok"] is True
        with Session(get_engine()) as s:
            card = s.get(models.Card, result["card_id"])
            assert card.comp_confidence == "medium"

    def test_comp_median_weighted_populated(self):
        p = _png(UPLOAD_DIR / "pcf_weighted_test.png", color=(30, 40, 50))
        ident = _fake_ident()
        comp = _fake_comp(median=20.0, median_recency_weighted=22.5, confidence="high")

        async def fid(*a, **kw): return ident
        async def fcomp(*a, **kw): return comp

        with patch.object(claude_vision, "identify_card_async", new=fid), \
             patch.object(comp_lookup, "fetch_comps", new=fcomp):
            result = asyncio.run(_process_one(p, None))

        with Session(get_engine()) as s:
            card = s.get(models.Card, result["card_id"])
            assert card.comp_median_weighted == 22.5

    def test_comp_suspicious_bulk_false_by_default(self):
        p = _png(UPLOAD_DIR / "pcf_notsusp_test.png", color=(60, 70, 80))
        ident = _fake_ident()
        comp = _fake_comp(suspicious_bulk=False)

        async def fid(*a, **kw): return ident
        async def fcomp(*a, **kw): return comp

        with patch.object(claude_vision, "identify_card_async", new=fid), \
             patch.object(comp_lookup, "fetch_comps", new=fcomp):
            result = asyncio.run(_process_one(p, None))

        with Session(get_engine()) as s:
            card = s.get(models.Card, result["card_id"])
            assert card.comp_suspicious_bulk is False
            assert "fingerprint" not in (card.notes or "")

    def test_suspicious_bulk_sets_flag_and_appends_note(self):
        p = _png(UPLOAD_DIR / "pcf_susp_test.png", color=(90, 100, 110))
        ident = _fake_ident()
        reason = "5 sales at $10.00 — possible bulk lot fingerprint"
        comp = _fake_comp(
            suspicious_bulk=True,
            suspicious_reason=reason,
            confidence="low",
        )

        async def fid(*a, **kw): return ident
        async def fcomp(*a, **kw): return comp

        with patch.object(claude_vision, "identify_card_async", new=fid), \
             patch.object(comp_lookup, "fetch_comps", new=fcomp):
            result = asyncio.run(_process_one(p, None))

        with Session(get_engine()) as s:
            card = s.get(models.Card, result["card_id"])
            assert card.comp_suspicious_bulk is True
            assert card.comp_suspicious_reason == reason
            assert "fingerprint" in (card.notes or "")
            assert "verify before pricing" in (card.notes or "")

    def test_suspicious_reason_stored(self):
        p = _png(UPLOAD_DIR / "pcf_reason_test.png", color=(120, 130, 140))
        ident = _fake_ident()
        reason = "7 sales at $3.50 — possible bulk lot fingerprint"
        comp = _fake_comp(suspicious_bulk=True, suspicious_reason=reason)

        async def fid(*a, **kw): return ident
        async def fcomp(*a, **kw): return comp

        with patch.object(claude_vision, "identify_card_async", new=fid), \
             patch.object(comp_lookup, "fetch_comps", new=fcomp):
            result = asyncio.run(_process_one(p, None))

        with Session(get_engine()) as s:
            card = s.get(models.Card, result["card_id"])
            assert card.comp_suspicious_reason == reason

    def test_effective_value_uses_weighted_median(self):
        """When comp_median_weighted is set, est_value_raw should reflect it."""
        p = _png(UPLOAD_DIR / "pcf_effval_test.png", color=(150, 160, 170))
        ident = _fake_ident()
        comp = _fake_comp(median=10.0, median_recency_weighted=15.0, confidence="medium")

        async def fid(*a, **kw): return ident
        async def fcomp(*a, **kw): return comp

        with patch.object(claude_vision, "identify_card_async", new=fid), \
             patch.object(comp_lookup, "fetch_comps", new=fcomp):
            result = asyncio.run(_process_one(p, None))

        with Session(get_engine()) as s:
            card = s.get(models.Card, result["card_id"])
            # est_value_raw should prefer the weighted median
            assert card.est_value_raw == 15.0

    def test_no_weighted_median_falls_back_to_plain(self):
        """When median_recency_weighted is None, est_value_raw = comp_median."""
        p = _png(UPLOAD_DIR / "pcf_fallback_test.png", color=(180, 190, 200))
        ident = _fake_ident()
        comp = _fake_comp(median=8.0, median_recency_weighted=None, confidence="low")

        async def fid(*a, **kw): return ident
        async def fcomp(*a, **kw): return comp

        with patch.object(claude_vision, "identify_card_async", new=fid), \
             patch.object(comp_lookup, "fetch_comps", new=fcomp):
            result = asyncio.run(_process_one(p, None))

        with Session(get_engine()) as s:
            card = s.get(models.Card, result["card_id"])
            assert card.est_value_raw == 8.0
