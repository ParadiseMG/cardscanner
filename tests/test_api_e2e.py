"""End-to-end pipeline test: upload an image, mock Claude+eBay, verify Card lands."""
import asyncio
import io
import json
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.main import app
from app.services import claude_vision, comp_lookup


client = TestClient(app)


def _png_bytes() -> bytes:
    img = Image.new("RGB", (200, 280), (255, 0, 128))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_health():
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json()["ok"]


def test_full_upload_pipeline():
    fake_id = claude_vision.CardIdentification(
        year=2018, set_brand="Bowman Chrome", player="Ronald Acuna Jr",
        card_no="BCP-25", parallel="Refractor", condition="NM",
        confidence=0.92,
    )
    fake_comp = comp_lookup.CompResult(
        query="2018 Bowman Chrome Acuna",
        url="https://example.com",
        prices=[60, 70, 80, 90, 100], median=80.0, low=60.0, high=100.0, count=5,
    )

    async def fake_identify(*a, **kw): return fake_id
    async def fake_fetch(*a, **kw): return fake_comp

    with patch.object(claude_vision, "identify_card_async", new=fake_identify), \
         patch.object(comp_lookup, "fetch_comps", new=fake_fetch):

        files = [("files", ("acuna.png", _png_bytes(), "image/png"))]
        r = client.post("/api/scans/upload", files=files,
                        headers={"X-Anthropic-Key": "sk-test"})
        assert r.status_code == 200, r.text
        job_id = r.json()["job_id"]

        # Wait for the background task -- TestClient runs in a thread,
        # so we need to give the loop time to settle.
        import time
        for _ in range(40):
            j = client.get(f"/api/scans/jobs/{job_id}").json()
            if j["status"] == "done":
                break
            time.sleep(0.25)
        assert j["status"] == "done"
        assert j["processed"] == 1, j

    # inventory should now have one card
    inv = client.get("/api/inventory").json()
    assert inv["total"] >= 1
    card = inv["items"][0]
    assert card["player"] == "Ronald Acuna Jr"
    assert card["comp_median"] == 80.0

    # stats reflect it
    stats = client.get("/api/stats").json()
    assert stats["total_cards"] >= 1
    assert stats["total_value"] >= 80.0

    # achievements pending have first_scan
    pend = client.get("/api/achievements/pending").json()
    codes = {a["code"] for a in pend["items"]}
    assert "first_scan" in codes


def test_listing_preview_and_inventory_patch():
    # First seed a card
    files = [("files", ("foo.png", _png_bytes(), "image/png"))]
    fake_id = claude_vision.CardIdentification(
        year=2018, set_brand="Bowman", player="Test Player",
        card_no="100", parallel="Base", condition="NM", confidence=0.9,
    )
    fake_comp = comp_lookup.CompResult(query="x", url="u", prices=[10],
                                       median=10.0, low=10.0, high=10.0, count=1)

    async def fake_identify(*a, **kw): return fake_id
    async def fake_fetch(*a, **kw): return fake_comp
    with patch.object(claude_vision, "identify_card_async", new=fake_identify), \
         patch.object(comp_lookup, "fetch_comps", new=fake_fetch):
        client.post("/api/scans/upload", files=files,
                    headers={"X-Anthropic-Key": "sk-test"})

    import time
    for _ in range(40):
        time.sleep(0.25)
        if client.get("/api/inventory").json()["total"] >= 1:
            break

    cid = client.get("/api/inventory").json()["items"][0]["id"]

    # Preview
    pv = client.get(f"/api/listings/preview/{cid}").json()
    assert pv["card_id"] == cid
    assert pv["format"] == "AUCTION"  # under $50
    assert "Test Player" in pv["title"]

    # Patch
    r = client.patch(f"/api/inventory/{cid}", json={"player": "Renamed Player", "status": "Ready"})
    assert r.status_code == 200
    assert r.json()["player"] == "Renamed Player"
    assert r.json()["status"] == "Ready"
