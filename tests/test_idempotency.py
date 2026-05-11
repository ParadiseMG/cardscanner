"""Re-running the pipeline on the same image produces 0 new cards."""
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
from app.services.drive_inbox import hash_file
from app.config import UPLOAD_DIR


def _png(path: Path, color=(255, 0, 0)) -> Path:
    Image.new("RGB", (200, 280), color).save(path)
    return path


def test_same_hash_does_not_double_insert():
    p = _png(UPLOAD_DIR / "ipd_test.png")
    h = hash_file(p)

    fake_id = claude_vision.CardIdentification(
        year=2018, set_brand="Bowman", player="Tester", card_no="1",
        condition="NM", confidence=0.9,
    )
    fake_comp = comp_lookup.CompResult(query="x", url="u", prices=[10],
                                       median=10.0, low=10.0, high=10.0, count=1)
    async def fid(*a, **kw): return fake_id
    async def fcomp(*a, **kw): return fake_comp

    with patch.object(claude_vision, "identify_card_async", new=fid), \
         patch.object(comp_lookup, "fetch_comps", new=fcomp):
        # First run inserts a card
        r1 = asyncio.run(_process_one(p, None, front_hash=h))
        assert r1["ok"] is True

        # Second run with same hash should skip
        r2 = asyncio.run(_process_one(p, None, front_hash=h))
        assert r2.get("skipped") is True

    with Session(get_engine()) as s:
        cards = s.exec(select(models.Card).where(models.Card.front_hash == h)).all()
        assert len(cards) == 1
