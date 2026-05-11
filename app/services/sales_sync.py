"""Sales sync service — pull eBay listing state transitions back into the dashboard.

Lifecycle
---------
1. `fetch_listing_status(offer_id)` — hit /sell/inventory/v1/offer/{offer_id} and
   return a normalised status dict.
2. `sync_listing(listing)` — pull latest state, update DB row.  When a sold
   transition fires, also update the parent Card and trigger xlsx/Sheets mirrors.
3. `sync_all_active()` — pull every Listing with status="active", fan out
   sync_listing with retry+backoff.  Throttles to ≤2 RPS.
"""
from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Optional

import httpx

from app.config import settings
from app import models
from app.db import get_engine, session_scope
from app.services import ebay_listing
from app.utils import logger as _log
from app.utils.retry import with_backoff

from sqlmodel import Session, select

log = _log.get(__name__)

# eBay offer status → our internal Listing.status vocabulary
_STATUS_MAP = {
    "UNPUBLISHED": "draft",
    "PUBLISHED": "active",
    "ENDED": "ended",
}

FEE_PCT_DEFAULT = 0.13


# ---------------------------------------------------------------------------
# A1a. fetch_listing_status
# ---------------------------------------------------------------------------

async def fetch_listing_status(offer_id: str) -> dict:
    """Call /sell/inventory/v1/offer/{offer_id} and return normalised status.

    Returns a dict with keys:
        status          — "draft" / "active" / "sold" / "ended"
        listing_id      — eBay listing ID (if published)
        current_price   — float
        sold_price      — float or None
        sold_at         — datetime or None
    """
    token = await ebay_listing._access_token()
    base = settings.ebay_api_base
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
        r = await client.get(f"{base}/sell/inventory/v1/offer/{offer_id}")
        r.raise_for_status()
        data = r.json()

    ebay_status = data.get("status", "UNPUBLISHED")
    internal_status = _STATUS_MAP.get(ebay_status, "draft")

    listing_info = data.get("listing", {})
    listing_id = listing_info.get("listingId")

    price_summary = data.get("pricingSummary", {})
    price_val = price_summary.get("price", {})
    try:
        current_price = float(price_val.get("value", 0))
    except (TypeError, ValueError):
        current_price = 0.0

    result: dict = {
        "status": internal_status,
        "listing_id": listing_id,
        "current_price": current_price,
        "sold_price": None,
        "sold_at": None,
    }

    # If published, check for a completed order via fulfillment API
    if listing_id and ebay_status == "PUBLISHED":
        sold_price, sold_at = await _check_for_sold_order(client, token, base, listing_id)
        if sold_price is not None:
            result["status"] = "sold"
            result["sold_price"] = sold_price
            result["sold_at"] = sold_at

    return result


async def _check_for_sold_order(
    client: httpx.AsyncClient,
    token: str,
    base: str,
    listing_id: str,
) -> tuple[Optional[float], Optional[datetime]]:
    """Check /sell/fulfillment/v1/order for a sold order matching listing_id.

    Returns (sold_price, sold_at) or (None, None).
    Handles both sandbox mock shape and production order shape.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }
    try:
        r = await client.get(
            f"{base}/sell/fulfillment/v1/order",
            headers=headers,
            params={"filter": "orderfulfillmentstatus:{NOT_STARTED|IN_PROGRESS|FULFILLED}"},
        )
        if r.status_code >= 400:
            return None, None
        data = r.json()
    except httpx.HTTPError:
        return None, None

    orders = data.get("orders", [])
    for order in orders:
        line_items = order.get("lineItems", [])
        for item in line_items:
            # Production shape: item has listingId or legacyItemId
            item_listing_id = (
                item.get("listingId")
                or item.get("legacyItemId")
                or item.get("lineItemId")
            )
            # Sandbox mock may just match on listing_id in the order itself
            order_listing_id = order.get("listingId") or order.get("legacyOrderId", "")

            matched = (
                str(item_listing_id) == str(listing_id)
                or str(order_listing_id) == str(listing_id)
            )
            if not matched:
                continue

            # Extract sold price
            total = order.get("pricingSummary", {})
            amount = total.get("total", total.get("priceSubtotal", {}))
            try:
                sold_price = float(amount.get("value", item.get("lineItemCost", {}).get("value", 0)))
            except (TypeError, ValueError):
                sold_price = 0.0

            # Extract sold datetime
            sold_at: Optional[datetime] = None
            closed_str = order.get("closedDate") or order.get("creationDate")
            if closed_str:
                try:
                    sold_at = datetime.fromisoformat(closed_str.replace("Z", "+00:00"))
                    sold_at = sold_at.replace(tzinfo=None)  # store as naive UTC
                except ValueError:
                    sold_at = datetime.utcnow()

            if sold_price and sold_price > 0:
                return sold_price, sold_at or datetime.utcnow()

    return None, None


# ---------------------------------------------------------------------------
# A1b. sync_listing
# ---------------------------------------------------------------------------

async def sync_listing(listing: models.Listing) -> bool:
    """Pull latest eBay state for one listing; update DB.

    Returns True if a sold transition was detected.
    """
    if not listing.offer_id:
        return False

    now = datetime.utcnow()
    try:
        status_data = await with_backoff(
            lambda: fetch_listing_status(listing.offer_id),
            attempts=3,
            base_delay=1.0,
            on_retry=lambda n, e: log.warning(
                "sync_listing retry",
                extra={"offer_id": listing.offer_id, "attempt": n, "error": str(e)},
            ),
        )
    except Exception as exc:
        # Record the error and move on
        with session_scope() as s:
            L = s.get(models.Listing, listing.id)
            if L:
                L.last_synced_at = now
                L.sync_error = str(exc)[:500]
                s.add(L)
        log.warning("sync_listing failed", extra={"listing_id": listing.id, "error": str(exc)})
        return False

    new_status = status_data["status"]
    sold_transition = (listing.status != "sold" and new_status == "sold")

    with session_scope() as s:
        L = s.get(models.Listing, listing.id)
        if not L:
            return False

        L.status = new_status
        L.last_synced_at = now
        L.sync_error = None

        if new_status == "active" and status_data.get("listing_id"):
            L.listing_id = status_data["listing_id"]

        if sold_transition:
            sp = status_data.get("sold_price") or L.price or 0.0
            sa = status_data.get("sold_at") or now
            L.sold_price = sp
            L.sold_at = sa
            L.fees = round(sp * FEE_PCT_DEFAULT, 2)

            # Update the parent Card
            card = s.get(models.Card, L.card_id)
            if card:
                card.status = "Sold"
                card.sold_price = sp
                card.sold_date = sa.date() if sa else None
                card.sold_at = sa
                card.sale_channel = "eBay"
                card.ebay_status = "sold"
                card.fee_pct = FEE_PCT_DEFAULT
                card.updated_at = now
                s.add(card)

                # Fire xlsx + Sheets mirrors in background
                asyncio.create_task(_mirror_sold(card.id))

        s.add(L)

    if sold_transition:
        log.info(
            "sync_listing sold transition",
            extra={"listing_id": listing.id, "sold_price": status_data.get("sold_price")},
        )

    return sold_transition


async def _mirror_sold(card_id: int) -> None:
    """Background task: update xlsx Sales Log and Sheets mirror when a card sells."""
    try:
        with Session(get_engine()) as s:
            card = s.get(models.Card, card_id)
            if not card:
                return
            # Detach a snapshot for use outside the session
            card_snapshot = models.Card(
                **{c.key: getattr(card, c.key) for c in card.__table__.columns}
            )

        from app.services import xlsx_mirror, sheets_sync
        xlsx_mirror.update_sales_log(card_snapshot)
        sheets_sync.append_sale(card_snapshot)
    except Exception as exc:
        log.warning("mirror_sold failed", extra={"card_id": card_id, "error": str(exc)})


# ---------------------------------------------------------------------------
# A1c. sync_all_active
# ---------------------------------------------------------------------------

async def sync_all_active() -> dict:
    """Pull every active Listing and run sync_listing with ≤2 RPS throttle.

    Returns {"checked": N, "transitions": N}.
    """
    with Session(get_engine()) as s:
        listings = s.exec(
            select(models.Listing).where(models.Listing.status == "active")
        ).all()

    if not listings:
        log.info("sync_all_active no active listings")
        return {"checked": 0, "transitions": 0}

    log.info("sync_all_active start", extra={"count": len(listings)})

    # Semaphore enforces ≤2 concurrent requests (≈2 RPS with network latency)
    sem = asyncio.Semaphore(2)
    checked = 0
    transitions = 0
    lock = asyncio.Lock()

    async def _run_one(listing: models.Listing) -> None:
        nonlocal checked, transitions
        async with sem:
            sold = await sync_listing(listing)
            async with lock:
                checked += 1
                if sold:
                    transitions += 1
            # Throttle: ensure we don't exceed 2 RPS
            await asyncio.sleep(0.5)

    tasks = [asyncio.create_task(_run_one(L)) for L in listings]
    await asyncio.gather(*tasks, return_exceptions=True)

    log.info("sync_all_active done", extra={"checked": checked, "transitions": transitions})
    return {"checked": checked, "transitions": transitions}
