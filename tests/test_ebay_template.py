"""eBay listing template builder — pure function, no API calls."""
from app import models
from app.services import ebay_listing


def _card(**kw) -> models.Card:
    base = dict(id=1, year=2018, set_brand="Bowman Chrome", player="Ronald Acuna Jr",
                card_no="BCP-25", parallel="Refractor", condition="NM",
                comp_median=80.0, comp_low=60.0, comp_high=110.0, comp_count=12)
    base.update(kw)
    return models.Card(**base)


def test_under_50_uses_auction():
    c = _card(comp_median=10.0)
    t = ebay_listing.build_listing_template(c)
    assert t["format"] == "AUCTION"
    assert t["duration"] == "DAYS_7"


def test_over_50_uses_fixed_price():
    c = _card(comp_median=120.0)
    t = ebay_listing.build_listing_template(c)
    assert t["format"] == "FIXED_PRICE"
    assert t["duration"] == "GTC"


def test_title_under_80_chars():
    c = _card(player="Some Player With A Very Very Long Name", parallel="Super Special Edition")
    t = ebay_listing.build_listing_template(c)
    assert len(t["title"]) <= 80


def test_aspects_include_year_and_player():
    c = _card()
    t = ebay_listing.build_listing_template(c)
    assert t["aspects"]["Year Manufactured"] == ["2018"]
    assert t["aspects"]["Player/Athlete"] == ["Ronald Acuna Jr"]
    assert t["aspects"]["Parallel/Variety"] == ["Refractor"]


def test_graded_card_marks_specific():
    c = _card(is_graded=True, grade="PSA 9", comp_median=300.0)
    t = ebay_listing.build_listing_template(c)
    assert t["condition"] == "GRADED"
    assert t["aspects"]["Graded"] == ["Yes"]
    assert "PSA" in t["aspects"]["Professional Grader"][0]


def test_fee_math():
    c = _card(comp_median=100.0)
    t = ebay_listing.build_listing_template(c)
    # 13% of price
    assert abs(t["expected_fees"] - round(t["price"] * 0.13, 2)) < 0.01
    assert abs(t["expected_net"] - round(t["price"] - t["expected_fees"], 2)) < 0.01
