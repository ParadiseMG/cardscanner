"""Diagnostic endpoints — probe what external APIs are actually returning.

Useful when a card returns no comps and you want to know if eBay served us
a captcha, an empty results page, or just genuinely no sold listings.
"""
from __future__ import annotations

from typing import Optional

import httpx
from bs4 import BeautifulSoup
from fastapi import APIRouter

from app.services import comp_lookup, browser_fetch

router = APIRouter()


@router.get("/diag/comp-probe")
async def comp_probe(
    year: Optional[int] = None,
    set_brand: Optional[str] = None,
    player: Optional[str] = None,
    card_no: Optional[str] = None,
    parallel: Optional[str] = "Base",
) -> dict:
    """Hit eBay sold-listings for the given query and report what we got back.

    Helpful when comp_median is None for cards that should clearly have sales.
    Tells you: status code, response size, whether selectors matched anything,
    a few sniff words from the page, and if it looks like a captcha/block.
    """
    query = comp_lookup.build_query(year, set_brand, player, card_no, parallel)
    url = comp_lookup.build_url(query)

    fetched_via = "none"
    html = ""
    status = 0

    # Try Playwright first
    if browser_fetch.is_available():
        pw_html = await browser_fetch.fetch_html(url)
        if pw_html:
            html = pw_html
            status = 200  # Playwright doesn't expose the response code; assume OK if we got HTML
            fetched_via = "playwright"

    # Fall back to httpx
    if not html:
        async with httpx.AsyncClient(timeout=20.0,
                                     headers=comp_lookup._build_headers(),
                                     follow_redirects=True) as client:
            try:
                r = await client.get(url)
                html = r.text
                status = r.status_code
                fetched_via = "httpx"
            except httpx.HTTPError as e:
                return {"query": query, "url": url, "error": str(e),
                        "fetched_via": "httpx_failed"}

    # Try each selector, report counts
    soup = BeautifulSoup(html, "html.parser")
    selector_hits = {sel: len(soup.select(sel)) for sel in comp_lookup._PRICE_SELECTORS}

    # Sniff for captcha / block phrases
    lc = html.lower()
    blocked_signals = []
    for needle in ("captcha", "robot", "verify you are human",
                   "unusual traffic", "blocked", "automated"):
        if needle in lc:
            blocked_signals.append(needle)

    # JSON-LD presence
    jsonld_count = len(soup.find_all("script", type="application/ld+json"))

    # Head sniff
    title_el = soup.find("title")
    title = title_el.get_text(strip=True) if title_el else ""

    # Run our actual extractor for comparison
    prices, source = comp_lookup.parse_prices_with_source_for_test(html)

    return {
        "query": query,
        "url": url,
        "status": status,
        "fetched_via": fetched_via,
        "playwright_available": browser_fetch.is_available(),
        "html_length": len(html),
        "title": title,
        "selector_hits": selector_hits,
        "jsonld_blocks": jsonld_count,
        "blocked_signals": blocked_signals,
        "extracted_prices_count": len(prices),
        "extracted_source": source,
        "first_5_prices": prices[:5],
    }


@router.post("/diag/clear-comp-cache")
def clear_comp_cache() -> dict:
    """Wipe both comp caches so the next request goes back out to eBay fresh."""
    ok = len(comp_lookup._OK_CACHE)
    fail = len(comp_lookup._FAIL_CACHE)
    comp_lookup._OK_CACHE.clear()
    comp_lookup._FAIL_CACHE.clear()
    return {"ok_entries_cleared": ok, "fail_entries_cleared": fail}
