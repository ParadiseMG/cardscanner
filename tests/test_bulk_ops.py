"""A6: Tests for bulk operations (A3)."""
from __future__ import annotations

from unittest.mock import patch, AsyncMock
import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app.main import app
from app import models
from app.db import get_engine

client = TestClient(app)


def _add_card(**kwargs) -> models.Card:
    defaults = dict(
        year=2020, set_brand="Topps", player="Bulk Player",
        status="Researching", ebay_status="not_listed",
        is_autograph=False, is_relic=False, is_graded=False,
        is_hit_watchlist=False, review_flagged=False,
        comp_median=10.0,
    )
    defaults.update(kwargs)
    with Session(get_engine()) as s:
        c = models.Card(**defaults)
        s.add(c)
        s.commit()
        s.refresh(c)
        return c


def _get_card(cid: int) -> models.Card:
    with Session(get_engine()) as s:
        return s.get(models.Card, cid)


# ---------------------------------------------------------------------------
# POST /api/bulk/ids
# ---------------------------------------------------------------------------

def test_bulk_ids_returns_matching():
    c1 = _add_card(player="AutoGuy", is_autograph=True)
    c2 = _add_card(player="BaseGuy", is_autograph=False)
    r = client.post("/api/bulk/ids", json={"autograph": True})
    assert r.status_code == 200
    data = r.json()
    assert c1.id in data["ids"]
    assert c2.id not in data["ids"]
    assert data["total"] == len(data["ids"])


def test_bulk_ids_excludes_deleted():
    c_del = _add_card(player="Deleted", status="Deleted")
    c_ok = _add_card(player="Visible", status="Researching")
    r = client.post("/api/bulk/ids", json={})
    data = r.json()
    assert c_ok.id in data["ids"]
    assert c_del.id not in data["ids"]


def test_bulk_ids_empty_filter_returns_all():
    c1 = _add_card(player="A")
    c2 = _add_card(player="B")
    r = client.post("/api/bulk/ids", json={})
    assert r.status_code == 200
    ids = r.json()["ids"]
    assert c1.id in ids
    assert c2.id in ids


# ---------------------------------------------------------------------------
# POST /api/bulk/patch
# ---------------------------------------------------------------------------

def test_bulk_patch_status():
    c1 = _add_card(player="P1", status="Researching")
    c2 = _add_card(player="P2", status="Researching")
    r = client.post("/api/bulk/patch", json={
        "ids": [c1.id, c2.id],
        "patch": {"status": "Ready"},
    })
    assert r.status_code == 200
    assert r.json()["updated"] == 2
    assert _get_card(c1.id).status == "Ready"
    assert _get_card(c2.id).status == "Ready"


def test_bulk_patch_channel():
    c = _add_card(player="P", channel=None)
    r = client.post("/api/bulk/patch", json={
        "ids": [c.id],
        "patch": {"channel": "eBay"},
    })
    assert r.status_code == 200
    assert _get_card(c.id).channel == "eBay"


def test_bulk_patch_notes_append():
    c = _add_card(player="P", notes="initial note")
    r = client.post("/api/bulk/patch", json={
        "ids": [c.id],
        "patch": {"notes_append": "extra note"},
    })
    assert r.status_code == 200
    updated = _get_card(c.id)
    assert "initial note" in updated.notes
    assert "extra note" in updated.notes


def test_bulk_patch_idempotent():
    """Applying the same patch twice should not error."""
    c = _add_card(player="P")
    body = {"ids": [c.id], "patch": {"status": "Ready"}}
    r1 = client.post("/api/bulk/patch", json=body)
    r2 = client.post("/api/bulk/patch", json=body)
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert _get_card(c.id).status == "Ready"


def test_bulk_patch_nonexistent_id_ignored():
    c = _add_card(player="Real")
    r = client.post("/api/bulk/patch", json={
        "ids": [c.id, 999999],
        "patch": {"status": "Ready"},
    })
    assert r.status_code == 200
    assert r.json()["updated"] == 1  # only the real card


# ---------------------------------------------------------------------------
# POST /api/bulk/delete (soft delete)
# ---------------------------------------------------------------------------

def test_bulk_soft_delete():
    c1 = _add_card(player="ToDelete1")
    c2 = _add_card(player="ToDelete2")
    c_keep = _add_card(player="Keep")
    r = client.post("/api/bulk/delete", json={"ids": [c1.id, c2.id]})
    assert r.status_code == 200
    assert r.json()["deleted"] == 2
    assert _get_card(c1.id).status == "Deleted"
    assert _get_card(c2.id).status == "Deleted"
    assert _get_card(c_keep.id).status != "Deleted"


def test_bulk_soft_delete_not_in_default_inventory():
    c = _add_card(player="GoneBye")
    client.post("/api/bulk/delete", json={"ids": [c.id]})
    r = client.get("/api/inventory")
    players = [i["player"] for i in r.json()["items"]]
    assert "GoneBye" not in players


def test_bulk_soft_delete_idempotent():
    c = _add_card(player="DoubleDelete")
    r1 = client.post("/api/bulk/delete", json={"ids": [c.id]})
    r2 = client.post("/api/bulk/delete", json={"ids": [c.id]})
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert _get_card(c.id).status == "Deleted"


# ---------------------------------------------------------------------------
# POST /api/bulk/recompute-comps
# ---------------------------------------------------------------------------

def test_bulk_recompute_queues_without_error():
    """Recompute should return queued count; no real eBay call made."""
    c1 = _add_card(player="P1")
    c2 = _add_card(player="P2")
    # Mock fetch_comps so no real HTTP call happens
    from app.services import comp_lookup
    fake_comp = comp_lookup.CompResult(
        query="x", url="u", prices=[20], median=20.0, low=20.0, high=20.0, count=1
    )
    async def fake_fetch(*a, **kw): return fake_comp

    with patch.object(comp_lookup, "fetch_comps", new=fake_fetch):
        r = client.post("/api/bulk/recompute-comps", json={"ids": [c1.id, c2.id]})

    assert r.status_code == 200
    assert r.json()["queued"] == 2


def test_bulk_recompute_nonexistent_id_not_queued():
    c = _add_card(player="Real")
    from app.services import comp_lookup
    async def fake_fetch(*a, **kw):
        return comp_lookup.CompResult(query="x", url="u", prices=[], median=None, low=None, high=None, count=0)
    with patch.object(comp_lookup, "fetch_comps", new=fake_fetch):
        r = client.post("/api/bulk/recompute-comps", json={"ids": [c.id, 999999]})
    assert r.json()["queued"] == 1  # only real card


# ---------------------------------------------------------------------------
# POST /api/bulk/move-to-bulk-lot
# ---------------------------------------------------------------------------

def test_bulk_move_to_lot():
    c1 = _add_card(player="P1")
    c2 = _add_card(player="P2")
    r = client.post("/api/bulk/move-to-bulk-lot", json={
        "ids": [c1.id, c2.id],
        "lot_label": "Box A",
    })
    assert r.status_code == 200
    assert r.json()["moved"] == 2
    assert _get_card(c1.id).status == "Bulk"
    assert _get_card(c2.id).status == "Bulk"
    assert "lot:Box A" in (_get_card(c1.id).notes or "")


def test_bulk_move_to_lot_idempotent():
    c = _add_card(player="P")
    body = {"ids": [c.id], "lot_label": "Box B"}
    r1 = client.post("/api/bulk/move-to-bulk-lot", json=body)
    r2 = client.post("/api/bulk/move-to-bulk-lot", json=body)
    assert r1.status_code == 200
    assert r2.status_code == 200
    # Note should not be duplicated
    notes = _get_card(c.id).notes or ""
    assert notes.count("lot:Box B") == 1
