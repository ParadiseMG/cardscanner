"""eBay Browse API — active-listing pricing.

Why this is better than scraping sold listings:
  - Free, no partner approval (Browse is a public API, just needs your prod
    app keyset registered on developer.ebay.com).
  - Active listings reflect TODAY's market — sold lists lag.
  - "Undercut the lowest active asking by N%" is exactly how flippers price.
  - No 403 / bot-detection arms race.

Auth: client_credentials grant — uses the same EBAY_APP_ID + EBAY_CERT_ID
already in your .env (no user consent needed for public data). The token
caches for ~2 hours so we hit eBay maybe ~12 times/day.

If `EBAY_ENV=sandbox` you'll get sparse seed data; for real prices flip to
`EBAY_ENV=production` once you've created a production keyset (also free,
takes minutes — no approval needed for Browse).
"""
from __future__ import annotations

import base64
import statistics
import time
from typing import Optional

import httpx

from app.config import settings
from app.utils import logger as _log

log = _log.get(__name__)

BROWSE_SCOPE = "https://api.ebay.com/oauth/api_scope"
_app_token_cache: dict = {"token": None, "expires_at": 0.0}


def is_configured() -> bool:
    return bool(settings.ebay_app_id and settings.ebay_cert_id)


async def _app_token() -> Optional[str]:
    """Get an app-only access token via client_credentials. Cached for ~2h."""
    if not is_configured():
        return None
    if (_app_token_cache["token"]
            and time.time() < _app_token_cache["expires_at"] - 60):
        return _app_token_cache["token"]
    auth = base64.b64encode(
        f"{settings.ebay_app_id}:{settings.ebay_cert_id}".encode()
    ).decode()
    url = f"{settings.ebay_api_base}/identity/v1/oauth2/token"
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.post(
                url,
                headers={
                    "Authorization": f"Basic {auth}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                data={"grant_type": "client_credentials", "scope": BROWSE_SCOPE},
            )
            if r.status_code != 200:
                log.warning("browse_api token fetch failed",
                            extra={"status": r.status_code, "body": r.text[:200]})
                return None
            data = r.json()
    except Exception as e:
        log.warning("browse_api token error", extra={"error": str(e)})
        return None
    _app_token_cache["token"] = data["access_token"]
    _app_token_cache["expires_at"] = time.time() + data.get("expires_in", 7200)
    return _app_token_cache["token"]


async def fetch_active_listings(
    query: str, *, limit: int = 50, condition: Optional[str] = None,
) -> list[float]:
    """Return active-listing USD prices for the query, or [] on failure.

    `condition` is an eBay condition filter string e.g. 'NEW|USED' (None = any).
    """
    token = await _app_token()
    if not token:
        return []
    url = f"{settings.ebay_api_base}/buy/browse/v1/item_summary/search"
    params: dict = {"q": query, "limit": str(limit)}
    filters = []
    if condition:
        filters.append(f"conditions:{{{condition}}}")
    if filters:
        params["filter"] = ",".join(filters)
    headers = {
        "Authorization": f"Bearer {token}",
        "X-EBAY-C-MARKETPLACE-ID": "EBAY_US",
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            r = await client.get(url, params=params, headers=headers)
        if r.status_code != 200:
            log.warning("browse_api non-200",
                        extra={"status": r.status_code, "body": r.text[:200]})
            return []
        data = r.json()
    except Exception as e:
        log.warning("browse_api fetch error", extra={"error": str(e)})
        return []

    out: list[float] = []
    for item in data.get("itemSummaries", []):
        price = item.get("price", {}) or {}
        val = price.get("value")
        if not val:
            continue
        try:
            out.append(float(val))
        except (TypeError, ValueError):
            continue
    return out


def suggest_price(active_prices: list[float], undercut_pct: float = 5.0) -> Optional[float]:
    """Suggested listing price = lowest active asking, undercut by N%.

    Floors at $0.99 (eBay minimum). Returns None if the input is empty.
    """
    if not active_prices:
        return None
    floor = min(active_prices)
    suggested = floor * (1.0 - undercut_pct / 100.0)
    return max(0.99, round(suggested, 2))
