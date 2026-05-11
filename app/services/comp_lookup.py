"""eBay sold-comps with selector fallbacks + JSON-LD + split caches.

The HTML structure of eBay's sold-listings page changes occasionally. We try
several selectors in order and warn (structured log) when none match, so we
notice drift before it silently breaks. As a final fallback, we extract any
JSON-LD `Product`/`Offer` blocks the page exposes and pull `price` from there.

Two caches:
  * `_OK_CACHE`     — successful (median, prices) lookups, 1h TTL
  * `_FAIL_CACHE`   — empty results, 5min TTL (so a transient eBay block doesn't
                       freeze us out for an hour)
"""
from __future__ import annotations

import json
import re
import statistics
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, List, Tuple

import httpx
from bs4 import BeautifulSoup

from app.utils import logger as _log
from app.utils.retry import with_backoff

log = _log.get(__name__)


@dataclass
class CompResult:
    query: str
    url: str
    prices: List[float] = field(default_factory=list)
    median: Optional[float] = None
    low: Optional[float] = None
    high: Optional[float] = None
    count: int = 0
    source: str = "none"  # which extractor won
    # B1 additions
    samples: List[Tuple[float, Optional[datetime]]] = field(default_factory=list)
    suspicious_bulk: bool = False
    suspicious_reason: str = ""
    median_recency_weighted: Optional[float] = None
    confidence: str = "low"  # high / medium / low


_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_5) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15 CardScanner/0.1"
)
_HEADERS = {
    "User-Agent": _USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

_PRICE_RE = re.compile(r"\$([0-9][0-9,]*\.?\d*)")
_OK_CACHE: dict[str, tuple[float, CompResult]] = {}
_FAIL_CACHE: dict[str, tuple[float, CompResult]] = {}
_OK_TTL = 60 * 60
_FAIL_TTL = 60 * 5

# Try multiple selector patterns; eBay rotates classnames sometimes.
_PRICE_SELECTORS = [
    "li.s-item .s-item__price",
    "li[data-listing-id] span.s-item__price",
    "div.s-item__details span.s-item__price",
    "span.POSITIVE",  # sometimes used on the new sold-listings template
]


def build_query(year, set_brand, player, card_no, parallel) -> str:
    bits = []
    if year:
        bits.append(str(year))
    if set_brand:
        bits.append(set_brand)
    if player:
        bits.append(player)
    if card_no:
        bits.append(f"#{card_no}")
    if parallel and parallel.lower() != "base":
        bits.append(parallel)
    return " ".join(bits).strip()


def build_url(query: str) -> str:
    return (
        "https://www.ebay.com/sch/i.html?"
        + urllib.parse.urlencode({
            "_nkw": query, "LH_Sold": 1, "LH_Complete": 1,
            "_ipg": 60, "_sop": 13,
        })
    )


# ---------------------------------------------------------------------------
# Extractors
# ---------------------------------------------------------------------------
def _prices_from_selectors(soup: BeautifulSoup) -> tuple[list[float], str]:
    for sel in _PRICE_SELECTORS:
        nodes = soup.select(sel)
        prices: list[float] = []
        for el in nodes:
            text = el.get_text(" ", strip=True)
            matches = _PRICE_RE.findall(text)
            if not matches:
                continue
            try:
                nums = [float(m.replace(",", "")) for m in matches]
                prices.append(statistics.mean(nums))
            except ValueError:
                continue
        if prices:
            return prices, sel
    log.warning("comp scraper: no selector matched", extra={"selectors": _PRICE_SELECTORS})
    return [], ""


def _prices_from_jsonld(soup: BeautifulSoup) -> list[float]:
    out: list[float] = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "{}")
        except (ValueError, TypeError):
            continue
        for item in (data if isinstance(data, list) else [data]):
            offer = (item.get("offers") if isinstance(item, dict) else None)
            if not offer:
                continue
            offers = offer if isinstance(offer, list) else [offer]
            for o in offers:
                p = o.get("price") if isinstance(o, dict) else None
                if p is None:
                    continue
                try:
                    out.append(float(p))
                except (ValueError, TypeError):
                    continue
    return out


def _trim_outliers(prices: list[float]) -> list[float]:
    """Internal alias; public name is filter_outliers."""
    return filter_outliers(prices)


# ---------------------------------------------------------------------------
# B1 pure-function helpers
# ---------------------------------------------------------------------------
def filter_outliers(prices: list[float]) -> list[float]:
    """For ≥10 prices, trim the bottom and top 10%. Otherwise return unchanged."""
    if len(prices) >= 10:
        prices = sorted(prices)
        cut = len(prices) // 10
        prices = prices[cut: len(prices) - cut]
    return prices


def detect_suspicious(prices: list[float]) -> tuple[bool, str]:
    """Return (suspicious, reason).

    Suspicious when ≥5 prices are within 1% of each other (bulk-lot fingerprint).
    """
    if len(prices) < 5:
        return False, ""
    # Sort and find the largest run within 1% of a reference price.
    sorted_p = sorted(prices)
    best_run: list[float] = []
    for ref in sorted_p:
        tol = ref * 0.01
        run = [p for p in sorted_p if abs(p - ref) <= tol]
        if len(run) > len(best_run):
            best_run = run
    if len(best_run) >= 5:
        avg = statistics.mean(best_run)
        reason = f"{len(best_run)} sales at ${avg:.2f} — possible bulk lot fingerprint"
        return True, reason
    return False, ""


def spread_ok(low: float, high: float, median: float) -> bool:
    """True when (high-low)/median ≤ 2.0 (low-spread pricing)."""
    if median == 0:
        return False
    return (high - low) / median <= 2.0


def recency_weighted(
    samples: list[tuple[float, Optional[datetime]]],
) -> Optional[float]:
    """Weighted median: last-7-days weight 3, last-30-days weight 2, older weight 1.

    If ANY sample lacks a date, falls back to plain median. Empty → None.
    """
    if not samples:
        return None
    # Fall back to plain median if any date is missing.
    if any(dt is None for _, dt in samples):
        return round(statistics.median([p for p, _ in samples]), 2)

    now = datetime.now(timezone.utc)
    weighted: list[float] = []
    for price, dt in samples:
        # Make dt timezone-aware if it's naive.
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        age_days = (now - dt).total_seconds() / 86400
        if age_days <= 7:
            w = 3
        elif age_days <= 30:
            w = 2
        else:
            w = 1
        weighted.extend([price] * w)
    return round(statistics.median(weighted), 2)


def _parse_prices(html: str) -> tuple[list[float], str, list[tuple[float, Optional[datetime]]]]:
    soup = BeautifulSoup(html, "html.parser")
    prices, source = _prices_from_selectors(soup)
    if not prices:
        prices = _prices_from_jsonld(soup)
        source = "jsonld" if prices else "none"
    prices = filter_outliers(prices)
    # Try to parse dates alongside prices.
    samples = _parse_samples(soup, prices, source)
    return prices, source, samples


def _parse_samples(
    soup: BeautifulSoup,
    prices: list[float],
    source: str,
) -> list[tuple[float, Optional[datetime]]]:
    """Best-effort: pair each price with a sale date from eBay HTML.

    eBay sold-listings expose dates in several ways:
      - data-time="<epoch-ms>" on the listing container
      - A span with class POSITIVE that contains text like "Sold  Dec 10, 2023"
    We're deliberately tolerant — if we can't match counts we return (price, None).
    """
    dates: list[Optional[datetime]] = []

    # Strategy 1: data-time attribute on listing containers
    containers = soup.select("li[data-time]") or soup.select("li.s-item[data-time]")
    if containers:
        for el in containers:
            raw = el.get("data-time", "")
            try:
                ts = int(raw)
                # eBay uses milliseconds
                dt = datetime.fromtimestamp(ts / 1000, tz=timezone.utc)
                dates.append(dt)
            except (ValueError, TypeError, OSError):
                dates.append(None)
    else:
        # Strategy 2: ".POSITIVE" spans containing "Sold" text
        pos_spans = soup.select("span.POSITIVE")
        sold_spans = [s for s in pos_spans if "sold" in s.get_text(strip=True).lower()]
        for span in sold_spans:
            text = span.get_text(" ", strip=True)
            dt = _parse_date_text(text)
            dates.append(dt)

    # Align: if we have exactly as many dates as prices, pair them up.
    if len(dates) == len(prices):
        return list(zip(prices, dates))
    # Otherwise return (price, None) for each price so we fall back gracefully.
    return [(p, None) for p in prices]


_DATE_RE = re.compile(
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s+\d{4}",
    re.IGNORECASE,
)


def _parse_date_text(text: str) -> Optional[datetime]:
    m = _DATE_RE.search(text)
    if not m:
        return None
    try:
        from datetime import datetime as _dt
        return _dt.strptime(m.group(0).replace(",", ""), "%b %d %Y").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Public
# ---------------------------------------------------------------------------
async def fetch_comps(
    year, set_brand, player, card_no, parallel,
    *, client: Optional[httpx.AsyncClient] = None,
) -> CompResult:
    query = build_query(year, set_brand, player, card_no, parallel)
    if not query:
        return CompResult(query="", url="", count=0)
    url = build_url(query)
    now = time.time()

    cached = _OK_CACHE.get(url)
    if cached and now - cached[0] < _OK_TTL:
        return cached[1]
    failed = _FAIL_CACHE.get(url)
    if failed and now - failed[0] < _FAIL_TTL:
        return failed[1]

    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=20.0, headers=_HEADERS, follow_redirects=True)
    try:
        async def _fetch() -> httpx.Response:
            r = await client.get(url, timeout=20.0)
            r.raise_for_status()
            return r
        resp = await with_backoff(_fetch, attempts=2, base_delay=0.5)
        prices, source, samples = _parse_prices(resp.text)
    except httpx.HTTPError as e:
        log.warning("comp fetch failed", extra={"url": url, "error": type(e).__name__})
        prices, source, samples = [], "error", []
    finally:
        if own:
            await client.aclose()

    res = CompResult(query=query, url=url, prices=prices, count=len(prices), source=source,
                     samples=samples)
    if prices:
        res.median = round(statistics.median(prices), 2)
        res.low = round(min(prices), 2)
        res.high = round(max(prices), 2)
        # B1: smarter comp analysis
        res.suspicious_bulk, res.suspicious_reason = detect_suspicious(prices)
        res.median_recency_weighted = recency_weighted(samples)
        # Confidence rating
        n = len(prices)
        if (n >= 10
                and not res.suspicious_bulk
                and res.low is not None
                and res.high is not None
                and res.median is not None
                and spread_ok(res.low, res.high, res.median)):
            res.confidence = "high"
        elif n >= 5:
            res.confidence = "medium"
        else:
            res.confidence = "low"
        _OK_CACHE[url] = (now, res)
    else:
        res.confidence = "low"
        _FAIL_CACHE[url] = (now, res)
    return res


def parse_prices_for_test(html: str) -> list[float]:
    return _parse_prices(html)[0]


def parse_prices_with_source_for_test(html: str) -> tuple[list[float], str]:
    prices, source, _ = _parse_prices(html)
    return prices, source


def reset_caches_for_test() -> None:
    _OK_CACHE.clear()
    _FAIL_CACHE.clear()


# B1 test hooks
def filter_outliers_for_test(prices: list[float]) -> list[float]:
    return filter_outliers(prices)


def detect_suspicious_for_test(prices: list[float]) -> tuple[bool, str]:
    return detect_suspicious(prices)


def recency_weighted_for_test(
    samples: list[tuple[float, Optional[datetime]]],
) -> Optional[float]:
    return recency_weighted(samples)
