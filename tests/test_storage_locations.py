"""Tests for the storage-locations API and the storage fields on cards.

Behavior covered:
  * Names dedupe case-insensitively and idempotently (POST is safe to retry).
  * Whitespace is normalized so 'Box  A' == 'Box A'.
  * Cards can be tagged with a location + free-text position via PATCH and bulk.
  * Bad location ids are rejected at the API boundary, not silently stored.
  * DELETE is protected when cards reference the location; ?force=true detaches.
  * Inventory ?storage_location_id= filter (and 0 == unassigned) works.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.main import app
from app import models
from app.db import get_engine


client = TestClient(app)


def _make_card(player: str = "Test Player") -> int:
    """Create a Card directly via the ORM and return its id."""
    with Session(get_engine()) as s:
        c = models.Card(year=1990, set_brand="Topps", player=player, card_no="1")
        s.add(c)
        s.commit()
        s.refresh(c)
        return c.id


def test_create_location_returns_201():
    r = client.post("/api/storage-locations",
                    json={"name": "Binder A", "kind": "binder"})
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "Binder A"
    assert body["kind"] == "binder"
    assert body["card_count"] == 0


def test_create_is_case_insensitive_idempotent():
    """POSTing the same name in different cases should return the first row, not create duplicates."""
    r1 = client.post("/api/storage-locations", json={"name": "Box A"})
    r2 = client.post("/api/storage-locations", json={"name": "box a"})
    r3 = client.post("/api/storage-locations", json={"name": "BOX A"})
    assert r1.json()["id"] == r2.json()["id"] == r3.json()["id"]
    # Display preserves whichever case won — the first one in.
    assert r1.json()["name"] == r2.json()["name"] == "Box A"
    listing = client.get("/api/storage-locations").json()
    assert len([loc for loc in listing if loc["name"].lower() == "box a"]) == 1


def test_create_normalizes_whitespace():
    """Extra/internal whitespace shouldn't create duplicate buckets."""
    r1 = client.post("/api/storage-locations", json={"name": "  Long Box 2 "})
    r2 = client.post("/api/storage-locations", json={"name": "Long  Box  2"})
    assert r1.json()["id"] == r2.json()["id"]
    assert r1.json()["name"] == "Long Box 2"


def test_create_empty_name_is_400():
    r = client.post("/api/storage-locations", json={"name": "   "})
    assert r.status_code == 400


def test_patch_renames_with_case_only_change():
    loc_id = client.post("/api/storage-locations",
                         json={"name": "Cabinet 1"}).json()["id"]
    r = client.patch(f"/api/storage-locations/{loc_id}",
                     json={"name": "CABINET 1"})
    assert r.status_code == 200
    assert r.json()["name"] == "CABINET 1"


def test_patch_rejects_rename_into_another_locations_name():
    a = client.post("/api/storage-locations", json={"name": "Spot A"}).json()["id"]
    b = client.post("/api/storage-locations", json={"name": "Spot B"}).json()["id"]
    r = client.patch(f"/api/storage-locations/{b}", json={"name": "spot a"})
    assert r.status_code == 409


def test_card_patch_sets_storage_location_and_position():
    loc_id = client.post("/api/storage-locations",
                         json={"name": "Binder Z"}).json()["id"]
    card_id = _make_card()
    r = client.patch(f"/api/inventory/{card_id}",
                     json={"storage_location_id": loc_id,
                           "storage_position": "p4/s7"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["storage_location_id"] == loc_id
    assert body["storage_location_name"] == "Binder Z"
    assert body["storage_position"] == "p4/s7"


def test_card_patch_rejects_unknown_location():
    card_id = _make_card()
    r = client.patch(f"/api/inventory/{card_id}",
                     json={"storage_location_id": 99999})
    assert r.status_code == 400


def test_card_patch_clears_location_with_null():
    loc_id = client.post("/api/storage-locations",
                         json={"name": "Temp"}).json()["id"]
    card_id = _make_card()
    client.patch(f"/api/inventory/{card_id}",
                 json={"storage_location_id": loc_id})
    r = client.patch(f"/api/inventory/{card_id}",
                     json={"storage_location_id": None})
    assert r.status_code == 200
    assert r.json()["storage_location_id"] is None


def test_inventory_filter_by_location():
    loc_id = client.post("/api/storage-locations",
                         json={"name": "Filter Test"}).json()["id"]
    matched = _make_card("In Location")
    _ = _make_card("Not In Location")  # untagged
    client.patch(f"/api/inventory/{matched}",
                 json={"storage_location_id": loc_id})

    listed = client.get(f"/api/inventory?storage_location_id={loc_id}").json()
    assert listed["total"] == 1
    assert listed["items"][0]["id"] == matched

    unassigned = client.get("/api/inventory?storage_location_id=0").json()
    ids = {c["id"] for c in unassigned["items"]}
    assert matched not in ids


def test_bulk_patch_moves_multiple_cards():
    loc_id = client.post("/api/storage-locations",
                         json={"name": "Bulk Target"}).json()["id"]
    a = _make_card("A")
    b = _make_card("B")
    r = client.post("/api/bulk/patch",
                    json={"ids": [a, b],
                          "patch": {"storage_location_id": loc_id,
                                    "storage_position": "row 1"}})
    assert r.status_code == 200
    assert r.json()["updated"] == 2

    card_a = client.get(f"/api/inventory/{a}").json()
    assert card_a["storage_location_id"] == loc_id
    assert card_a["storage_position"] == "row 1"


def test_bulk_patch_rejects_unknown_location():
    a = _make_card()
    r = client.post("/api/bulk/patch",
                    json={"ids": [a], "patch": {"storage_location_id": 88888}})
    assert r.status_code == 400


def test_delete_protected_when_cards_reference():
    loc_id = client.post("/api/storage-locations",
                         json={"name": "In Use"}).json()["id"]
    card_id = _make_card()
    client.patch(f"/api/inventory/{card_id}",
                 json={"storage_location_id": loc_id})

    r = client.delete(f"/api/storage-locations/{loc_id}")
    assert r.status_code == 409


def test_delete_with_force_detaches_cards():
    loc_id = client.post("/api/storage-locations",
                         json={"name": "Force Test"}).json()["id"]
    card_id = _make_card()
    client.patch(f"/api/inventory/{card_id}",
                 json={"storage_location_id": loc_id})

    r = client.delete(f"/api/storage-locations/{loc_id}?force=true")
    assert r.status_code == 200
    assert r.json()["detached_cards"] == 1

    card = client.get(f"/api/inventory/{card_id}").json()
    assert card["storage_location_id"] is None


def test_listing_includes_card_count():
    loc_id = client.post("/api/storage-locations",
                         json={"name": "Counting"}).json()["id"]
    card_id = _make_card()
    client.patch(f"/api/inventory/{card_id}",
                 json={"storage_location_id": loc_id})
    locs = {loc["id"]: loc for loc in client.get("/api/storage-locations").json()}
    assert locs[loc_id]["card_count"] == 1


# ---------------------------------------------------------------------------
# m11: per-sync storage tag — cards created by the sync inherit the tag
# ---------------------------------------------------------------------------

def test_process_one_inherits_storage_from_job(monkeypatch):
    """A Card created by a job-running worker takes the job's location tag."""
    import asyncio
    from datetime import datetime as _dt
    from sqlmodel import Session
    from app import pipeline, models
    from app.db import get_engine
    from app.services import claude_vision, comp_lookup

    loc_id = client.post("/api/storage-locations",
                         json={"name": "PipelineTest"}).json()["id"]

    # Create a ScanJob carrying the storage tag.
    with Session(get_engine()) as s:
        job = models.ScanJob(
            label="test", total=1,
            storage_location_id=loc_id,
            storage_position="p9/s9",
        )
        s.add(job); s.commit(); s.refresh(job)
        job_id = job.id

    # Stub out vision + comp + image normalize so the test runs offline and fast.
    fake_id = claude_vision.CardIdentification(
        year=1990, set_brand="Topps", player="Test Player", card_no="1",
        parallel="Base", condition="NM", confidence=0.9,
    )
    fake_comp = comp_lookup.CompResult(
        query="q", url="u", prices=[1.0], count=1, source="test",
    )
    fake_comp.median = 1.0; fake_comp.low = 1.0; fake_comp.high = 1.0

    async def fake_identify(*a, **kw): return fake_id
    async def fake_comps(*a, **kw): return fake_comp
    monkeypatch.setattr(claude_vision, "identify_card_async", fake_identify)
    monkeypatch.setattr(comp_lookup, "fetch_comps", fake_comps)
    monkeypatch.setattr(pipeline, "normalize_image", lambda p: p)

    from pathlib import Path
    res = asyncio.get_event_loop().run_until_complete(
        pipeline._process_one(
            Path("/tmp/fake.jpg"),
            api_key_override=None,
            front_hash=None,
            job_id=job_id,
        )
    )
    assert res.get("ok") is True
    card_id = res["card_id"]

    card = client.get(f"/api/inventory/{card_id}").json()
    assert card["storage_location_id"] == loc_id
    assert card["storage_location_name"] == "PipelineTest"
    assert card["storage_position"] == "p9/s9"
