"""A5: Tests for app/services/sales_sync.py — mock eBay HTTP throughout."""
from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlmodel import Session

from app.db import get_engine
from app import models
from app.services import sales_sync


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _add_card(**kwargs) -> models.Card:
    defaults = dict(
        player="Test Player",
        year=2020,
        set_brand="Topps",
        status="Listed",
        ebay_status="active",
        comp_median=25.0,
    )
    defaults.update(kwargs)
    card = models.Card(**defaults)
    with Session(get_engine()) as s:
        s.add(card)
        s.commit()
        s.refresh(card)
    return card


def _add_listing(card_id: int, **kwargs) -> models.Listing:
    defaults = dict(
        card_id=card_id,
        status="active",
        offer_id="offer-001",
        listing_id="v1|1234|0",
        price=25.0,
    )
    defaults.update(kwargs)
    listing = models.Listing(**defaults)
    with Session(get_engine()) as s:
        s.add(listing)
        s.commit()
        s.refresh(listing)
    return listing


def _mock_offer_response(status="PUBLISHED", listing_id="v1|1234|0", price=25.0) -> dict:
    return {
        "status": status,
        "listing": {"listingId": listing_id},
        "pricingSummary": {"price": {"value": str(price), "currency": "USD"}},
    }


def _mock_order_response(listing_id="v1|1234|0", sold_price=24.50) -> dict:
    return {
        "orders": [
            {
                "orderId": "ord-111",
                "listingId": listing_id,
                "closedDate": "2026-05-10T12:00:00Z",
                "pricingSummary": {"total": {"value": str(sold_price), "currency": "USD"}},
                "lineItems": [
                    {
                        "listingId": listing_id,
                        "lineItemCost": {"value": str(sold_price), "currency": "USD"},
                    }
                ],
            }
        ]
    }


# ---------------------------------------------------------------------------
# Tests for fetch_listing_status
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_listing_status_active():
    """PUBLISHED offer with no orders → status=active."""
    with patch.object(sales_sync.ebay_listing, "_access_token", new=AsyncMock(return_value="t")):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _mock_offer_response(status="PUBLISHED")
        mock_resp.raise_for_status = MagicMock()

        empty_orders = MagicMock()
        empty_orders.status_code = 200
        empty_orders.json.return_value = {"orders": []}

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(side_effect=[mock_resp, empty_orders])

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await sales_sync.fetch_listing_status("offer-001")

    assert result["status"] == "active"
    assert result["listing_id"] == "v1|1234|0"
    assert result["current_price"] == 25.0
    assert result["sold_price"] is None


@pytest.mark.asyncio
async def test_fetch_listing_status_unpublished():
    """UNPUBLISHED offer → status=draft."""
    with patch.object(sales_sync.ebay_listing, "_access_token", new=AsyncMock(return_value="t")):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = _mock_offer_response(status="UNPUBLISHED")
        mock_resp.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(return_value=mock_resp)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await sales_sync.fetch_listing_status("offer-001")

    assert result["status"] == "draft"


@pytest.mark.asyncio
async def test_fetch_listing_status_sold_via_order():
    """PUBLISHED offer with matching order → status=sold, sold_price populated."""
    with patch.object(sales_sync.ebay_listing, "_access_token", new=AsyncMock(return_value="t")):
        mock_offer = MagicMock()
        mock_offer.status_code = 200
        mock_offer.json.return_value = _mock_offer_response(status="PUBLISHED")
        mock_offer.raise_for_status = MagicMock()

        mock_orders = MagicMock()
        mock_orders.status_code = 200
        mock_orders.json.return_value = _mock_order_response(sold_price=24.50)

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client.get = AsyncMock(side_effect=[mock_offer, mock_orders])

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await sales_sync.fetch_listing_status("offer-001")

    assert result["status"] == "sold"
    assert result["sold_price"] == 24.50
    assert result["sold_at"] is not None


# ---------------------------------------------------------------------------
# Tests for sync_listing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sync_listing_sold_transition():
    """sync_listing flips listing and card to sold when eBay reports sold."""
    card = _add_card()
    listing = _add_listing(card.id)

    fake_status = {
        "status": "sold",
        "listing_id": "v1|1234|0",
        "current_price": 25.0,
        "sold_price": 22.00,
        "sold_at": datetime(2026, 5, 10, 12, 0, 0),
    }

    with patch.object(sales_sync, "fetch_listing_status", new=AsyncMock(return_value=fake_status)):
        with patch.object(sales_sync, "_mirror_sold", new=AsyncMock()):
            sold = await sales_sync.sync_listing(listing)

    assert sold is True

    with Session(get_engine()) as s:
        updated_listing = s.get(models.Listing, listing.id)
        updated_card = s.get(models.Card, card.id)

    assert updated_listing.status == "sold"
    assert updated_listing.sold_price == 22.00
    assert updated_listing.fees is not None
    assert updated_card.status == "Sold"
    assert updated_card.sold_price == 22.00
    assert updated_card.sale_channel == "eBay"
    assert updated_card.fee_pct == pytest.approx(0.13)


@pytest.mark.asyncio
async def test_sync_listing_no_transition_stays_active():
    """sync_listing returns False when listing stays active."""
    card = _add_card()
    listing = _add_listing(card.id)

    fake_status = {
        "status": "active",
        "listing_id": "v1|1234|0",
        "current_price": 25.0,
        "sold_price": None,
        "sold_at": None,
    }

    with patch.object(sales_sync, "fetch_listing_status", new=AsyncMock(return_value=fake_status)):
        sold = await sales_sync.sync_listing(listing)

    assert sold is False

    with Session(get_engine()) as s:
        updated_listing = s.get(models.Listing, listing.id)
        updated_card = s.get(models.Card, card.id)

    assert updated_listing.status == "active"
    assert updated_card.status == "Listed"  # unchanged


@pytest.mark.asyncio
async def test_sync_listing_records_error_on_failure():
    """sync_listing records sync_error when eBay call fails."""
    card = _add_card()
    listing = _add_listing(card.id)

    import httpx
    with patch.object(
        sales_sync, "fetch_listing_status",
        new=AsyncMock(side_effect=httpx.HTTPStatusError(
            "500 error", request=MagicMock(), response=MagicMock(status_code=500)
        )),
    ):
        sold = await sales_sync.sync_listing(listing)

    assert sold is False

    with Session(get_engine()) as s:
        updated_listing = s.get(models.Listing, listing.id)

    assert updated_listing.sync_error is not None
    assert updated_listing.last_synced_at is not None


@pytest.mark.asyncio
async def test_sync_listing_no_offer_id():
    """sync_listing returns False immediately if listing has no offer_id."""
    card = _add_card()
    listing = _add_listing(card.id, offer_id=None)

    sold = await sales_sync.sync_listing(listing)
    assert sold is False


# ---------------------------------------------------------------------------
# Tests for sync_all_active
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_sync_all_active_no_listings():
    """sync_all_active returns zero counts when no active listings exist."""
    result = await sales_sync.sync_all_active()
    assert result == {"checked": 0, "transitions": 0}


@pytest.mark.asyncio
async def test_sync_all_active_transitions():
    """sync_all_active processes all active listings and counts transitions."""
    card1 = _add_card(player="Player A")
    card2 = _add_card(player="Player B")
    listing1 = _add_listing(card1.id, offer_id="offer-A")
    listing2 = _add_listing(card2.id, offer_id="offer-B")

    async def fake_sync(listing):
        return listing.offer_id == "offer-A"  # only listing1 transitions

    with patch.object(sales_sync, "sync_listing", new=AsyncMock(side_effect=fake_sync)):
        result = await sales_sync.sync_all_active()

    assert result["checked"] == 2
    assert result["transitions"] == 1


@pytest.mark.asyncio
async def test_sync_all_active_retry_on_5xx():
    """with_backoff retries on 5xx before raising; sync_listing handles error gracefully."""
    import httpx
    card = _add_card()
    listing = _add_listing(card.id)

    call_count = 0

    async def flaky_fetch(offer_id):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            raise httpx.HTTPStatusError(
                "503", request=MagicMock(), response=MagicMock(status_code=503)
            )
        return {
            "status": "active", "listing_id": "v1|1234|0",
            "current_price": 25.0, "sold_price": None, "sold_at": None,
        }

    with patch.object(sales_sync, "fetch_listing_status", new=AsyncMock(side_effect=flaky_fetch)):
        sold = await sales_sync.sync_listing(listing)

    assert call_count == 3  # retried twice before succeeding
    assert sold is False
