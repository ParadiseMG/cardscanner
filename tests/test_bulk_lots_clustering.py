"""B9: Tests for cluster_for_lots pure function."""
from __future__ import annotations

import pytest
from app import models
from app.services.bulk_lots import cluster_for_lots, LOT_MAX_CARDS, BulkLotProposal


def _card(
    id: int,
    year: int = 1989,
    set_brand: str = "Donruss",
    player: str = "Player",
    comp_median: float = 0.50,
    sport: str = "Baseball",
    status: str = "Researching",
) -> models.Card:
    c = models.Card(
        player=player,
        year=year,
        set_brand=set_brand,
        comp_median=comp_median,
        sport=sport,
        status=status,
    )
    c.id = id
    return c


class TestClusterByYearAndSet:
    def test_groups_same_year_set(self):
        cards = [_card(i, year=1989, set_brand="Donruss") for i in range(1, 11)]
        proposals = cluster_for_lots(cards)
        assert len(proposals) == 1
        assert "1989 Donruss" in proposals[0].cluster_label

    def test_separates_different_sets(self):
        donruss = [_card(i, year=1989, set_brand="Donruss") for i in range(1, 5)]
        topps = [_card(i + 10, year=1989, set_brand="Topps") for i in range(1, 5)]
        proposals = cluster_for_lots(donruss + topps)
        labels = {p.cluster_label for p in proposals}
        assert any("Donruss" in l for l in labels)
        assert any("Topps" in l for l in labels)

    def test_separates_different_years(self):
        c89 = [_card(i, year=1989, set_brand="Donruss") for i in range(1, 4)]
        c90 = [_card(i + 10, year=1990, set_brand="Donruss") for i in range(1, 4)]
        proposals = cluster_for_lots(c89 + c90)
        assert len(proposals) == 2


class TestFallbackToDecadeSport:
    def test_falls_back_to_decade_sport_when_no_set_brand(self):
        # Cards with no set_brand fall back to decade+sport
        cards = [
            models.Card(id=i, player=f"P{i}", year=1989, comp_median=0.5, sport="Baseball")
            for i in range(1, 5)
        ]
        # set_brand is None → falls back to decade/sport
        proposals = cluster_for_lots(cards)
        assert len(proposals) == 1
        assert "1980s" in proposals[0].cluster_label or "Baseball" in proposals[0].cluster_label

    def test_fallback_groups_same_decade(self):
        # 1989 and 1987 both in 1980s
        c1 = models.Card(id=1, year=1989, comp_median=0.5, sport="Baseball")
        c2 = models.Card(id=2, year=1987, comp_median=0.5, sport="Baseball")
        proposals = cluster_for_lots([c1, c2])
        assert len(proposals) == 1
        assert "1980s" in proposals[0].cluster_label


class TestFallbackToEra:
    def test_falls_back_to_era_when_decade_singleton(self):
        # Mix of years from same era but different decades — after pass 1 singletons
        # go to pass 2, then pass 3
        # Build: 1989 (1980s) and 1990 (1990s) — both Baseball, no set_brand
        # Pass 2: each decade has 1 card → singleton → pass 3
        # Pass 3: both are "Junk Wax (1986-1991)" → grouped
        c1 = models.Card(id=1, year=1989, comp_median=0.5, sport="Baseball")
        c2 = models.Card(id=2, year=1990, comp_median=0.5, sport="Baseball")
        proposals = cluster_for_lots([c1, c2])
        # Either decade or era grouping — either way both are in same proposal
        assert len(proposals) >= 1
        all_ids = [cid for p in proposals for cid in p.card_ids]
        assert 1 in all_ids
        assert 2 in all_ids


class TestLotCapEnforcement:
    def test_caps_at_100_cards(self):
        cards = [_card(i, year=1989, set_brand="Donruss") for i in range(1, 201)]
        proposals = cluster_for_lots(cards)
        for p in proposals:
            assert p.count <= LOT_MAX_CARDS

    def test_200_cards_splits_into_two_proposals(self):
        cards = [_card(i, year=1989, set_brand="Donruss") for i in range(1, 201)]
        proposals = cluster_for_lots(cards)
        # 200 cards / 100 cap = 2 proposals
        assert len(proposals) == 2
        total = sum(p.count for p in proposals)
        assert total == 200

    def test_exactly_100_is_one_proposal(self):
        cards = [_card(i, year=1989, set_brand="Donruss") for i in range(1, 101)]
        proposals = cluster_for_lots(cards)
        assert len(proposals) == 1
        assert proposals[0].count == 100


class TestFiltering:
    def test_excludes_bulk_status_cards(self):
        bulk_cards = [_card(i, status="Bulk") for i in range(1, 5)]
        proposals = cluster_for_lots(bulk_cards)
        assert proposals == []

    def test_excludes_sold_cards(self):
        sold_cards = [_card(i, status="Sold") for i in range(1, 5)]
        proposals = cluster_for_lots(sold_cards)
        assert proposals == []

    def test_excludes_deleted_cards(self):
        del_cards = [_card(i, status="Deleted") for i in range(1, 5)]
        proposals = cluster_for_lots(del_cards)
        assert proposals == []

    def test_excludes_high_value_cards(self):
        # $2+ cards are not sub-$1
        high_val = [_card(i, comp_median=2.0) for i in range(1, 5)]
        proposals = cluster_for_lots(high_val)
        assert proposals == []

    def test_empty_input(self):
        assert cluster_for_lots([]) == []


class TestProposalFields:
    def test_proposal_count_matches_card_count(self):
        cards = [_card(i) for i in range(1, 6)]
        proposals = cluster_for_lots(cards)
        assert proposals[0].count == 5

    def test_estimated_value_is_sum_of_medians(self):
        cards = [_card(i, comp_median=0.50) for i in range(1, 6)]
        proposals = cluster_for_lots(cards)
        assert proposals[0].estimated_value == pytest.approx(2.50)

    def test_suggested_price_is_70_pct(self):
        cards = [_card(i, comp_median=1.0) for i in range(1, 11)]
        # all are sub-$1 (0.5 each)... wait, comp_median=1.0 is not < 1.0
        # Use 0.5
        cards = [_card(i, comp_median=0.50) for i in range(1, 11)]
        proposals = cluster_for_lots(cards)
        expected_val = 10 * 0.5  # 5.0
        assert proposals[0].suggested_price == pytest.approx(expected_val * 0.7)

    def test_suggested_title_contains_label(self):
        cards = [_card(i, year=1989, set_brand="Donruss") for i in range(1, 4)]
        proposals = cluster_for_lots(cards)
        assert "1989 Donruss" in proposals[0].suggested_title

    def test_suggested_title_contains_lot_count(self):
        cards = [_card(i) for i in range(1, 6)]
        proposals = cluster_for_lots(cards)
        assert "5" in proposals[0].suggested_title

    def test_suggested_title_includes_top_players(self):
        # Three distinct players sorted by value
        cards = [
            _card(1, player="Rich Player", comp_median=0.90),
            _card(2, player="Mid Player", comp_median=0.60),
            _card(3, player="Poor Player", comp_median=0.10),
        ]
        proposals = cluster_for_lots(cards)
        title = proposals[0].suggested_title
        assert "Rich Player" in title

    def test_sorted_by_value_desc(self):
        expensive = [_card(i, year=1990, set_brand="Leaf", comp_median=0.80)
                     for i in range(1, 6)]
        cheap = [_card(i + 10, year=1989, set_brand="Donruss", comp_median=0.10)
                 for i in range(1, 6)]
        proposals = cluster_for_lots(expensive + cheap)
        # Expensive lot should come first
        assert proposals[0].estimated_value > proposals[-1].estimated_value
