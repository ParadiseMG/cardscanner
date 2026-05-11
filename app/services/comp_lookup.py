"""Pull recent SOLD comps for a card from eBay's public completed-listings page.

Why scrape and not use the API: eBay's Marketplace Insights API is gated to
approved partners (they've been turning down small-developer apps in 2026).
The public sold/completed page is publicly indexable and returns the same
data; we read it server-side, parse with BeautifulSoup, and compute the
median. Results are cached in-memory for an hour to avoid hammering eBay.

The returned URL is the human-friendly sold-listings search page, the same
URL Connor was already pasting into his Comps URL column.
"""
from __future__ import annotations

import re
import statistics
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Optional, List

import httpx
from bs4 import BeautifulSoup


@dataclass
class CompResult:
    query: str
    url: str
    prices: List[float] = field(default_factory=list)
    median: Optional[float] = None
    low: Optional[float] = None
    high: Optional[float] = None
    count: int = 0


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
_CACHE: dict[str, tuple[float, CompResult]] = {}
_CACHE_TTL_SECONDS = 60 * 60


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
            "_nkw": query,
            "LH_Sold": 1,
            "LH_Complete": 1,
            "_ipg": 60,
            "_sop": 13,  # newly sold
        })
    )


def _parse_prices(html: str) -> list[float]:
    soup = BeautifulSoup(html, "html.parser")
    out: list[float] = []
    # eBay sold listings put each item in <li class="s-item ...">.
    # Price is in <span class="s-item__price">.
    for el in soup.select("li.s-item .s-item__price"):
        text = el.get_text(" ", strip=True)
        # ranges like "$10.00 to $20.00" -> take midpoint
        matches = _PRICE_RE.findall(text)
        if not matches:
            continue
        try:
            nums = [float(m.replace(",", "")) for m in matches]
            out.append(statistics.mean(nums))
        except ValueError:
            continue
    # filter obvious junk: drop top/bottom 10% if we have >=10 samples
    if len(out) >= 10:
        out.sort()
        cut = len(out) // 10
        out = out[cut: len(out) - cut]
    return out


async def fetch_comps(
    year, set_brand, player, card_no, parallel,
    *, client: Optional[httpx.AsyncClient] = None,
) -> CompResult:
    query = build_query(year, set_brand, player, card_no, parallel)
    if not query:
        return CompResult(query="", url="", count=0)
    url = build_url(query)

    now = time.time()
    cached = _CACHE.get(url)
    if cached and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]

    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=20.0, headers=_HEADERS, follow_redirects=True)
    try:
        resp = await client.get(url)
        resp.raise_for_status()
        prices = _parse_prices(resp.text)
    except httpx.HTTPError:
        prices = []
    finally:
        if own:
            await client.aclose()

    res = CompResult(query=query, url=url, prices=prices, count=len(prices))
    if prices:
        res.median = round(statistics.median(prices), 2)
        res.low = round(min(prices), 2)
        res.high = round(max(prices), 2)
    _CACHE[url] = (now, res)
    return res


def parse_prices_for_test(html: str) -> list[float]:
    """Exposed so tests can run without network."""
    return _parse_prices(html)
