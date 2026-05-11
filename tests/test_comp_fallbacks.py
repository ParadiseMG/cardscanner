"""Comp scraper selector + JSON-LD fallback."""
from app.services import comp_lookup


def test_jsonld_fallback_used_when_no_selector_matches():
    html = """
    <html><body>
    <script type="application/ld+json">
    [{"@type": "Product", "offers": [{"price": "12.50"}, {"price": "15.00"}, {"price": "18.99"}]}]
    </script>
    </body></html>
    """
    prices, source = comp_lookup.parse_prices_with_source_for_test(html)
    assert source == "jsonld"
    assert 12.50 in prices and 15.0 in prices and 18.99 in prices


def test_alternate_selector_works():
    # No s-item__price, but the new POSITIVE-class fallback should catch it.
    html = """
    <html><body>
    <ul>
      <li><span class="POSITIVE">$8.00</span></li>
      <li><span class="POSITIVE">$10.00</span></li>
    </ul>
    </body></html>
    """
    prices, source = comp_lookup.parse_prices_with_source_for_test(html)
    assert source == "span.POSITIVE"
    assert 8.0 in prices and 10.0 in prices


def test_empty_html_yields_none():
    prices, source = comp_lookup.parse_prices_with_source_for_test("<html></html>")
    assert prices == []
    assert source == "none"
