"""B9: Tests for PSA and SGC CSV builders."""
from __future__ import annotations

import csv
import io

import pytest
from app import models
from app.services.grading import build_psa_csv, build_sgc_csv


def _card(
    player: str = "Mike Trout",
    year: int = 2011,
    set_brand: str = "Topps Update",
    card_no: str = "US175",
    parallel: str = "Base",
    notes: str = "",
    comp_median: float = 100.0,
) -> models.Card:
    c = models.Card(
        player=player,
        year=year,
        set_brand=set_brand,
        card_no=card_no,
        parallel=parallel,
        notes=notes,
        comp_median=comp_median,
    )
    return c


def _parse_csv(text: str) -> list[dict]:
    reader = csv.DictReader(io.StringIO(text))
    return list(reader)


class TestBuildPsaCsv:
    def test_header_columns_match_psa_spec(self):
        cards = [_card()]
        text = build_psa_csv(cards, service_level="Value")
        reader = csv.reader(io.StringIO(text))
        headers = next(reader)
        assert headers == ["Item", "Year", "Brand", "Player", "Variant",
                           "Card #", "Service Level", "Notes"]

    def test_single_card_row(self):
        cards = [_card(player="Ken Griffey Jr", year=1989,
                       set_brand="Upper Deck", card_no="1", parallel="Base")]
        rows = _parse_csv(build_psa_csv(cards, service_level="Value"))
        assert len(rows) == 1
        assert rows[0]["Player"] == "Ken Griffey Jr"
        assert rows[0]["Year"] == "1989"
        assert rows[0]["Brand"] == "Upper Deck"
        assert rows[0]["Card #"] == "1"
        assert rows[0]["Variant"] == "Base"
        assert rows[0]["Service Level"] == "Value"

    def test_item_numbering_starts_at_1(self):
        cards = [_card(), _card(player="Barry Bonds")]
        rows = _parse_csv(build_psa_csv(cards))
        assert rows[0]["Item"] == "1"
        assert rows[1]["Item"] == "2"

    def test_service_level_propagated(self):
        cards = [_card()]
        rows = _parse_csv(build_psa_csv(cards, service_level="Express"))
        assert rows[0]["Service Level"] == "Express"

    def test_multiple_cards(self):
        cards = [_card(player=f"Player {i}") for i in range(5)]
        rows = _parse_csv(build_psa_csv(cards))
        assert len(rows) == 5

    def test_special_chars_escaped_in_player(self):
        # Player name with a comma — csv module should quote it
        cards = [_card(player='Smith, Jr., Bob')]
        text = build_psa_csv(cards)
        rows = _parse_csv(text)
        assert rows[0]["Player"] == 'Smith, Jr., Bob'

    def test_notes_field_included(self):
        cards = [_card(notes="Possible RC")]
        rows = _parse_csv(build_psa_csv(cards))
        assert rows[0]["Notes"] == "Possible RC"

    def test_empty_card_list(self):
        text = build_psa_csv([])
        rows = _parse_csv(text)
        assert rows == []

    def test_null_year_becomes_empty_string(self):
        card = models.Card(player="Unknown", year=None)
        text = build_psa_csv([card])
        rows = _parse_csv(text)
        assert rows[0]["Year"] == ""

    def test_returns_string(self):
        text = build_psa_csv([_card()])
        assert isinstance(text, str)


class TestBuildSgcCsv:
    def test_header_columns_match_sgc_spec(self):
        cards = [_card()]
        text = build_sgc_csv(cards)
        reader = csv.reader(io.StringIO(text))
        headers = next(reader)
        assert headers == ["Item", "Year", "Brand", "Player", "Set Variation",
                           "Card Number", "Declared Value", "Notes"]

    def test_declared_value_from_comp_median(self):
        cards = [_card(comp_median=47.50)]
        rows = _parse_csv(build_sgc_csv(cards))
        assert rows[0]["Declared Value"] == "47.50"

    def test_single_row_basic_fields(self):
        cards = [_card(player="Babe Ruth", year=1933, set_brand="Goudey")]
        rows = _parse_csv(build_sgc_csv(cards))
        assert rows[0]["Player"] == "Babe Ruth"
        assert rows[0]["Year"] == "1933"
        assert rows[0]["Brand"] == "Goudey"

    def test_item_numbering(self):
        cards = [_card(), _card(player="Aaron Judge")]
        rows = _parse_csv(build_sgc_csv(cards))
        assert rows[0]["Item"] == "1"
        assert rows[1]["Item"] == "2"

    def test_special_chars_in_notes(self):
        cards = [_card(notes='Note with "quotes" and, commas')]
        text = build_sgc_csv(cards)
        rows = _parse_csv(text)
        assert rows[0]["Notes"] == 'Note with "quotes" and, commas'

    def test_returns_string(self):
        assert isinstance(build_sgc_csv([_card()]), str)
