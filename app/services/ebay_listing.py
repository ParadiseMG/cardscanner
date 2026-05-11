"""eBay Sell-API integration: OAuth + listing draft + publish.

Phasing
-------
Phase 1 (this MVP): point at eBay SANDBOX. The user gets sandbox credentials
instantly from https://developer.ebay.com/my/keys -- no approval gate.
Phase 2 (Connor): swap EBAY_ENV=production once eBay approves the production
keyset (1-3 days). Same code path, different base URLs.

Lifecycle for a listing
-----------------------
1. createOrReplaceInventoryItem  (sku = our card id)
2. createOffer                   (price, format, policies)
3. publishOffer                  (goes live; returns a listingId)

Photos: uploaded via the Picture Services / `/sell/inventory/v1/inventory_item/SKU/picture`
helper (we use the inventory-item image URL list, supplying signed image URLs we
host locally OR uploaded to eBay EPS). For sandbox we accept that the EPS
endpoint may reject local URLs; we fall back to eBay's stock placeholder so the
draft round-trip still succeeds.

Fee math (approx)
-----------------
Insertion fee: free for first 250 listings/mo; $0.30 thereafter.
Final value fee: ~13.25% on trading-card singles (varies; we use 13% as the
display estimate).
"""
from __future__ import annotations

import base64
import json
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx

from app.config import settings
from app import models


SCOPES = [
    "https://api.ebay.com/oauth/api_scope/sell.inventory",
    "https://api.ebay.com/oauth/api_scope/sell.account.readonly",
    "https://api.ebay.com/oauth/api_scope/sell.marketing",
    "https://api.ebay.com/oauth/api_scope/sell.fulfillment",
]
DEFAULT_CATEGORY_BASEBALL_SINGLE = "213"  # Sports Mem > Trading Cards > Baseball


# ---------------------------------------------------------------------------
# OAuth helpers
# ---------------------------------------------------------------------------
def _basic_auth_header() -> dict:
    raw = f"{settings.ebay_app_id}:{settings.ebay_cert_id}".encode()
    return {"Authorization": "Basic " + base64.b64encode(raw).decode()}


def is_configured() -> bool:
    return bool(settings.ebay_app_id and settings.ebay_cert_id and settings.ebay_runame)


def is_connected() -> bool:
    return Path(settings.ebay_token_path).exists()


def begin_auth() -> str:
    """Return the full eBay OAuth authorization URL."""
    if not is_configured():
        raise RuntimeError(
            "eBay app credentials missing. Add EBAY_APP_ID/EBAY_CERT_ID/EBAY_RUNAME to .env."
        )
    params = {
        "client_id": settings.ebay_app_id,
        "response_type": "code",
        "redirect_uri": settings.ebay_runame,
        "scope": " ".join(SCOPES),
        "state": secrets.token_urlsafe(16),
    }
    return f"{settings.ebay_oauth_authorize_url}?{urllib.parse.urlencode(params)}"


async def finish_auth(code: str) -> dict:
    """Exchange authorization code for refresh+access token; persist."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            settings.ebay_oauth_token_url,
            headers={**_basic_auth_header(), "Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": settings.ebay_runame,
            },
        )
        r.raise_for_status()
        tok = r.json()
    tok["obtained_at"] = int(time.time())
    Path(settings.ebay_token_path).write_text(json.dumps(tok, indent=2))
    return tok


async def _access_token() -> str:
    p = Path(settings.ebay_token_path)
    if not p.exists():
        raise RuntimeError("eBay not connected — start /auth/ebay/start first.")
    tok = json.loads(p.read_text())
    if tok.get("obtained_at", 0) + tok.get("expires_in", 0) - 60 > time.time():
        return tok["access_token"]
    # refresh
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            settings.ebay_oauth_token_url,
            headers={**_basic_auth_header(), "Content-Type": "application/x-www-form-urlencoded"},
            data={
                "grant_type": "refresh_token",
                "refresh_token": tok["refresh_token"],
                "scope": " ".join(SCOPES),
            },
        )
        r.raise_for_status()
        new = r.json()
    tok.update(new)
    tok["obtained_at"] = int(time.time())
    p.write_text(json.dumps(tok, indent=2))
    return tok["access_token"]


# ---------------------------------------------------------------------------
# Policy readiness check
# ---------------------------------------------------------------------------
@dataclass
class PolicyCheck:
    ready: bool
    missing: list[str] = field(default_factory=list)
    fulfillment_id: Optional[str] = None
    payment_id: Optional[str] = None
    return_id: Optional[str] = None


async def check_seller_policies() -> PolicyCheck:
    """Hit /sell/account/v1/* and confirm at least one policy of each type exists."""
    if settings.ebay_fulfillment_policy_id and settings.ebay_payment_policy_id and settings.ebay_return_policy_id:
        return PolicyCheck(
            ready=True,
            fulfillment_id=settings.ebay_fulfillment_policy_id,
            payment_id=settings.ebay_payment_policy_id,
            return_id=settings.ebay_return_policy_id,
        )
    try:
        token = await _access_token()
    except Exception:
        return PolicyCheck(ready=False, missing=["not_connected"])
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    base = settings.ebay_api_base
    pc = PolicyCheck(ready=True)
    async with httpx.AsyncClient(timeout=30.0, headers=headers) as client:
        for kind, attr in [("fulfillment", "fulfillment_id"),
                           ("payment", "payment_id"),
                           ("return", "return_id")]:
            try:
                r = await client.get(
                    f"{base}/sell/account/v1/{kind}_policy",
                    params={"marketplace_id": "EBAY_US"},
                )
                if r.status_code == 200:
                    items = r.json().get(f"{kind}Policies", [])
                    if items:
                        setattr(pc, attr, items[0].get(f"{kind}PolicyId"))
                        continue
                pc.missing.append(kind)
                pc.ready = False
            except httpx.HTTPError:
                pc.missing.append(kind)
                pc.ready = False
    return pc


# ---------------------------------------------------------------------------
# Listing template builder
# ---------------------------------------------------------------------------
def build_listing_template(card: models.Card) -> dict:
    """Pure function: turn a Card into an editable listing draft (preview)."""
    title = card.display_title()
    if card.condition:
        title += f" - {card.condition}"
    title = title[:80]  # eBay max

    median = card.comp_median or 0.0
    is_auction = median < 50
    fmt = "AUCTION" if is_auction else "FIXED_PRICE"
    price = round(median * 0.85, 2) if is_auction else round(median, 2)
    if price < 0.99:
        price = 0.99

    shipping = (
        "USPS Ground Advantage $4.99"
        if median >= 20 else
        "eBay Standard Envelope $0.71"
    )

    # Item specifics aspect map
    aspects: dict[str, list[str]] = {
        "Sport": [card.sport or "Baseball"],
        "Manufacturer": [card.set_brand or "Unknown"] if card.set_brand else [],
        "Year Manufactured": [str(card.year)] if card.year else [],
        "Player/Athlete": [card.player] if card.player else [],
        "Card Number": [card.card_no] if card.card_no else [],
        "Parallel/Variety": [card.parallel] if card.parallel else [],
        "Card Condition": [_ebay_condition(card)],
        "Autographed": ["Yes" if card.is_autograph else "No"],
        "Features": [],
    }
    if card.is_relic:
        aspects["Features"].append("Relic")
    if card.is_autograph:
        aspects["Features"].append("Autograph")
    if card.team:
        aspects["Team"] = [card.team]
    if card.is_graded and card.grade:
        aspects["Graded"] = ["Yes"]
        aspects["Professional Grader"] = [card.grade.split()[0]]
        aspects["Grade"] = [card.grade]
    aspects = {k: v for k, v in aspects.items() if v}

    description = _build_description(card)

    fee_pct = 0.13
    expected_fees = round(price * fee_pct, 2)
    expected_net = round(price - expected_fees, 2)

    return {
        "card_id": card.id,
        "title": title,
        "format": fmt,
        "duration": "DAYS_7" if is_auction else "GTC",
        "price": price,
        "currency": "USD",
        "category_id": DEFAULT_CATEGORY_BASEBALL_SINGLE,
        "condition": _ebay_condition_enum(card),
        "aspects": aspects,
        "description": description,
        "shipping_default": shipping,
        "comp_median": median,
        "comp_low": card.comp_low,
        "comp_high": card.comp_high,
        "comp_count": card.comp_count,
        "expected_fees": expected_fees,
        "expected_net": expected_net,
    }


def _ebay_condition(card: models.Card) -> str:
    if card.is_graded:
        return f"Graded {card.grade or ''}".strip()
    return {
        "NM": "Near Mint or Better",
        "EX": "Excellent",
        "VG": "Very Good",
        "GD": "Good",
        "PR": "Poor",
    }.get((card.condition or "").upper(), card.condition or "Ungraded")


def _ebay_condition_enum(card: models.Card) -> str:
    # eBay condition IDs for trading cards
    if card.is_graded:
        return "GRADED"
    return {
        "NM": "NEAR_MINT_OR_BETTER",
        "EX": "EXCELLENT",
        "VG": "VERY_GOOD",
        "GD": "GOOD",
        "PR": "POOR",
    }.get((card.condition or "").upper(), "UNGRADED")


def _build_description(card: models.Card) -> str:
    lines = [
        f"<h2>{card.display_title()}</h2>",
        "<p>Listing auto-generated by CardScanner.</p>",
        "<ul>",
    ]
    if card.year:
        lines.append(f"<li><b>Year:</b> {card.year}</li>")
    if card.set_brand:
        lines.append(f"<li><b>Set:</b> {card.set_brand}</li>")
    if card.player:
        lines.append(f"<li><b>Player:</b> {card.player}</li>")
    if card.card_no:
        lines.append(f"<li><b>Card #:</b> {card.card_no}</li>")
    if card.parallel and card.parallel.lower() != "base":
        lines.append(f"<li><b>Parallel/Variety:</b> {card.parallel}</li>")
    lines.append(f"<li><b>Condition:</b> {_ebay_condition(card)}</li>")
    if card.is_autograph:
        lines.append("<li><b>Certified autograph.</b></li>")
    if card.is_relic:
        lines.append("<li><b>Relic / patch card.</b></li>")
    lines.append("</ul>")
    if card.comp_count and card.comp_median:
        lines.append(
            f"<p>Recent eBay sold range: ${card.comp_low:.2f} - ${card.comp_high:.2f} "
            f"(median ${card.comp_median:.2f}, {card.comp_count} sales).</p>"
        )
    lines.append("<p>Card shipped securely in a penny sleeve and toploader.</p>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Publish via Sell API (Phase 1 stops at createOffer in sandbox)
# ---------------------------------------------------------------------------
@dataclass
class PublishResult:
    success: bool
    sku: str
    offer_id: Optional[str] = None
    listing_id: Optional[str] = None
    error: Optional[str] = None


async def publish_listing(card: models.Card, draft: dict, publish: bool = False) -> PublishResult:
    """Create the inventory item + offer; optionally publish.

    `publish=False` (default in sandbox) stops after createOffer so the user
    can verify the round-trip without putting anything live.
    """
    sku = f"CS-{card.id:06d}"
    try:
        token = await _access_token()
    except Exception as e:
        return PublishResult(success=False, sku=sku, error=str(e))

    base = settings.ebay_api_base
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Content-Language": "en-US",
        "Accept-Language": "en-US",
    }
    aspects = {k: v for k, v in draft["aspects"].items() if v}

    inventory_body = {
        "product": {
            "title": draft["title"],
            "description": draft["description"],
            "aspects": aspects,
            # placeholder image URL list -- in production we POST images to EPS
            # and use the returned hosted URLs. Sandbox accepts any URL.
            "imageUrls": ["https://i.ebayimg.com/images/g/placeholder/s-l1600.jpg"],
        },
        "condition": draft["condition"],
        "availability": {"shipToLocationAvailability": {"quantity": 1}},
    }

    offer_body = {
        "sku": sku,
        "marketplaceId": "EBAY_US",
        "format": draft["format"],
        "availableQuantity": 1,
        "categoryId": draft["category_id"],
        "listingDescription": draft["description"],
        "pricingSummary": {
            "price": {"value": str(draft["price"]), "currency": draft["currency"]},
        },
        "listingDuration": draft["duration"],
        "listingPolicies": {
            "fulfillmentPolicyId": settings.ebay_fulfillment_policy_id,
            "paymentPolicyId": settings.ebay_payment_policy_id,
            "returnPolicyId": settings.ebay_return_policy_id,
        },
        "merchantLocationKey": settings.ebay_merchant_location_key,
    }

    async with httpx.AsyncClient(timeout=45.0, headers=headers) as client:
        r1 = await client.put(
            f"{base}/sell/inventory/v1/inventory_item/{sku}",
            json=inventory_body,
        )
        if r1.status_code >= 400:
            return PublishResult(success=False, sku=sku, error=f"inventory_item: {r1.text}")

        r2 = await client.post(f"{base}/sell/inventory/v1/offer", json=offer_body)
        if r2.status_code >= 400:
            return PublishResult(success=False, sku=sku, error=f"offer: {r2.text}")
        offer_id = r2.json().get("offerId")

        if not publish:
            return PublishResult(success=True, sku=sku, offer_id=offer_id)

        r3 = await client.post(f"{base}/sell/inventory/v1/offer/{offer_id}/publish")
        if r3.status_code >= 400:
            return PublishResult(success=False, sku=sku, offer_id=offer_id, error=f"publish: {r3.text}")
        listing_id = r3.json().get("listingId")
        return PublishResult(success=True, sku=sku, offer_id=offer_id, listing_id=listing_id)
