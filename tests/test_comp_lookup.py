"""Pure-function tests for comp parsing — no network."""
from app.services import comp_lookup


SAMPLE_HTML = """
<html><body>
<ul>
  <li class="s-item"><span class="s-item__price">$12.50</span></li>
  <li class="s-item"><span class="s-item__price">$15.00</span></li>
  <li class="s-item"><span class="s-item__price">$10.00 to $20.00</span></li>
  <li class="s-item"><span class="s-item__price">$1,250.00</span></li>
  <li class="s-item"><span class="s-item__price">$8.99</span></li>
</ul>
</body></html>
"""


def test_parse_prices_extracts_floats():
    prices = comp_lookup.parse_prices_for_test(SAMPLE_HTML)
    assert 12.50 in prices
    assert 15.00 in prices
    assert 8.99 in prices
    # range averaged
    assert 15.00 in prices  # 10+20/2 also = 15
    # large value
    assert 1250.00 in prices


def test_build_query_skips_base():
    q = comp_lookup.build_query(2018, "Bowman Chrome", "Acuna", "BCP-25", "Base")
    assert "Base" not in q
    assert "2018" in q and "Acuna" in q


def test_build_query_includes_parallel():
    q = comp_lookup.build_query(2018, "Bowman", "Acuna", "BCP-25", "Refractor")
    assert "Refractor" in q


def test_build_url_has_sold_filters():
    url = comp_lookup.build_url("2018 Bowman Acuna")
    assert "LH_Sold=1" in url and "LH_Complete=1" in url
