"""A5: Tests for /api/sales/* endpoints."""
from __future__ import annotations

from datetime import datetime, date

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session

from app.db import get_engine
from app import models
from app.main import app

client = TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _card(
    status="Sold",
    sold_price=20.0,
    fee_pct=0.13,
    sale_channel="eBay",
    sold_at: datetime | None = None,
    player: str = "Test Player",
    year: int = 2020,
    set_brand: str = "Topps",
    ebay_status: str | None = None,
    created_at: datetime | None = None,
    **kwargs,
) -> models.Card:
    if ebay_status is None:
        ebay_status = "sold" if status == "Sold" else "not_listed"
    _sold_at = sold_at or (datetime(2026, 5, 1, 10, 0, 0) if status == "Sold" else None)
    # created_at defaults to before sold_at so sell_through calc works
    _created_at = created_at or datetime(2026, 4, 20, 10, 0, 0)
    c = models.Card(
        player=player,
        year=year,
        set_brand=set_brand,
        status=status,
        sold_price=sold_price,
        fee_pct=fee_pct,
        sale_channel=sale_channel,
        sold_at=_sold_at,
        ebay_status=ebay_status,
        created_at=_created_at,
        **kwargs,
    )
    with Session(get_engine()) as s:
        s.add(c)
        s.commit()
        s.refresh(c)
    return c


def _listing(card_id: int, status="active", price=25.0, **kwargs) -> models.Listing:
    L = models.Listing(card_id=card_id, status=status, price=price, **kwargs)
    with Session(get_engine()) as s:
        s.add(L)
        s.commit()
        s.refresh(L)
    return L


# ---------------------------------------------------------------------------
# POST /api/sales/sync
# ---------------------------------------------------------------------------

def test_sync_returns_queued():
    resp = client.post("/api/sales/sync")
    assert resp.status_code == 200
    assert resp.json()["queued"] is True


# ---------------------------------------------------------------------------
# GET /api/sales/summary — empty DB
# ---------------------------------------------------------------------------

def test_summary_empty_db():
    resp = client.get("/api/sales/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert data["realized_total"] == 0.0
    assert data["realized_net"] == 0.0
    assert data["fees_total"] == 0.0
    assert data["by_month"] == {}
    assert data["by_channel"] == {}
    assert data["active_value"] == 0.0
    assert data["draft_value"] == 0.0
    assert data["sell_through_days_avg"] is None


def test_summary_with_sold_cards():
    """Summary math: 3 sold cards at $20 each, 13% fee."""
    for _ in range(3):
        _card(sold_price=20.0, fee_pct=0.13, sale_channel="eBay",
              sold_at=datetime(2026, 5, 1))

    resp = client.get("/api/sales/summary")
    assert resp.status_code == 200
    data = resp.json()

    assert data["realized_total"] == pytest.approx(60.0)
    assert data["fees_total"] == pytest.approx(7.80, abs=0.01)
    assert data["realized_net"] == pytest.approx(52.20, abs=0.01)
    assert "2026-05" in data["by_month"]
    assert data["by_month"]["2026-05"]["count"] == 3
    assert "eBay" in data["by_channel"]
    assert data["by_channel"]["eBay"]["count"] == 3


def test_summary_active_value():
    """Active listing values roll up to active_value."""
    c = _card(status="Listed", sold_price=None, fee_pct=None, sale_channel=None,
              sold_at=None, ebay_status="active")
    _listing(c.id, status="active", price=30.0)
    _listing(c.id, status="active", price=15.0)

    resp = client.get("/api/sales/summary")
    data = resp.json()
    assert data["active_value"] == pytest.approx(45.0)


def test_summary_draft_value():
    """Draft listing values roll up to draft_value."""
    c = _card(status="Researching", sold_price=None, fee_pct=None, sale_channel=None,
              sold_at=None, ebay_status="not_listed")
    _listing(c.id, status="draft", price=10.0)

    resp = client.get("/api/sales/summary")
    data = resp.json()
    assert data["draft_value"] == pytest.approx(10.0)


def test_summary_by_month_multi():
    """Multiple months appear correctly in by_month."""
    _card(sold_price=50.0, sale_channel="eBay", sold_at=datetime(2026, 4, 15))
    _card(sold_price=30.0, sale_channel="FB",   sold_at=datetime(2026, 5, 5))

    resp = client.get("/api/sales/summary")
    data = resp.json()
    assert "2026-04" in data["by_month"]
    assert "2026-05" in data["by_month"]
    assert data["by_month"]["2026-04"]["gross"] == pytest.approx(50.0)
    assert data["by_month"]["2026-05"]["gross"] == pytest.approx(30.0)


def test_summary_sell_through_avg():
    """sell_through_days_avg is populated when sold cards have created_at."""
    _card(sold_price=20.0, sold_at=datetime(2026, 5, 11))
    resp = client.get("/api/sales/summary")
    data = resp.json()
    # Should be a non-negative number
    assert data["sell_through_days_avg"] is not None
    assert data["sell_through_days_avg"] >= 0


# ---------------------------------------------------------------------------
# GET /api/sales/recent
# ---------------------------------------------------------------------------

def test_recent_returns_sold_cards_in_order():
    """Recent sales are ordered by sold_at DESC."""
    c1 = _card(player="Alpha", sold_at=datetime(2026, 5, 1))
    c2 = _card(player="Beta",  sold_at=datetime(2026, 5, 3))
    c3 = _card(player="Gamma", sold_at=datetime(2026, 5, 2))

    resp = client.get("/api/sales/recent?n=10")
    assert resp.status_code == 200
    items = resp.json()
    players = [i["player"] for i in items]
    # Beta (May 3) should be first
    assert players.index("Beta") < players.index("Gamma") < players.index("Alpha")


def test_recent_respects_n_param():
    for i in range(5):
        _card(player=f"Player {i}", sold_at=datetime(2026, 5, i + 1))

    resp = client.get("/api/sales/recent?n=3")
    assert resp.status_code == 200
    assert len(resp.json()) == 3


def test_recent_only_sold():
    """Non-sold cards are not included in recent sales."""
    _card(status="Listed", sold_price=None, fee_pct=None, sale_channel=None,
          sold_at=None, ebay_status="active", player="NotSold")
    _card(player="IsSold", sold_at=datetime(2026, 5, 1), status="Sold")

    resp = client.get("/api/sales/recent?n=20")
    players = [i["player"] for i in resp.json()]
    assert "IsSold" in players
    assert "NotSold" not in players


def test_recent_net_fee_calc():
    """Net and fees are computed correctly in recent response."""
    _card(sold_price=100.0, fee_pct=0.13, sold_at=datetime(2026, 5, 1))

    resp = client.get("/api/sales/recent?n=1")
    item = resp.json()[0]
    assert item["sold_price"] == 100.0
    assert item["fees"] == pytest.approx(13.0)
    assert item["net"] == pytest.approx(87.0)


# ---------------------------------------------------------------------------
# POST /api/sales/{listing_id}/mark-sold-manual
# ---------------------------------------------------------------------------

def test_mark_sold_manual_basic():
    """Manual mark-sold updates listing and card, returns net."""
    c = _card(status="Listed", sold_price=None, fee_pct=None, sale_channel=None,
              sold_at=None, ebay_status="active", player="Mark Sold Test")
    L = _listing(c.id, status="active", price=25.0)

    resp = client.post(f"/api/sales/{L.id}/mark-sold-manual", json={
        "sold_price": 22.0,
        "channel": "eBay",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["sold_price"] == 22.0
    assert data["fees"] == pytest.approx(2.86, abs=0.01)
    assert data["net"] == pytest.approx(19.14, abs=0.01)
    assert data["channel"] == "eBay"

    with Session(get_engine()) as s:
        updated_listing = s.get(models.Listing, L.id)
        updated_card = s.get(models.Card, c.id)

    assert updated_listing.status == "sold"
    assert updated_listing.sold_price == 22.0
    assert updated_card.status == "Sold"
    assert updated_card.sale_channel == "eBay"


def test_mark_sold_manual_custom_fees():
    """Custom fee value overrides default calculation."""
    c = _card(status="Listed", sold_price=None, fee_pct=None, sale_channel=None,
              sold_at=None, ebay_status="active", player="Custom Fee Test")
    L = _listing(c.id, status="active", price=50.0)

    resp = client.post(f"/api/sales/{L.id}/mark-sold-manual", json={
        "sold_price": 50.0,
        "fees": 5.00,
        "channel": "LCS",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["fees"] == 5.0
    assert data["net"] == pytest.approx(45.0)
    assert data["channel"] == "LCS"


def test_mark_sold_manual_fb_channel():
    """Facebook marketplace channel is recorded correctly."""
    c = _card(status="Listed", sold_price=None, fee_pct=None, sale_channel=None,
              sold_at=None, ebay_status="active", player="FB Channel Test")
    L = _listing(c.id, status="active")

    client.post(f"/api/sales/{L.id}/mark-sold-manual", json={
        "sold_price": 15.0,
        "channel": "FB",
    })

    with Session(get_engine()) as s:
        updated_card = s.get(models.Card, c.id)
    assert updated_card.sale_channel == "FB"


def test_mark_sold_manual_not_found():
    """404 when listing doesn't exist."""
    resp = client.post("/api/sales/99999/mark-sold-manual", json={"sold_price": 10.0})
    assert resp.status_code == 404


def test_mark_sold_manual_custom_sold_at():
    """Custom sold_at datetime is stored."""
    c = _card(status="Listed", sold_price=None, fee_pct=None, sale_channel=None,
              sold_at=None, ebay_status="active", player="Custom SoldAt Test")
    L = _listing(c.id)

    custom_time = "2026-04-15T09:30:00"
    resp = client.post(f"/api/sales/{L.id}/mark-sold-manual", json={
        "sold_price": 25.0,
        "channel": "eBay",
        "sold_at": custom_time,
    })
    assert resp.status_code == 200
    assert "2026-04-15" in resp.json()["sold_at"]
