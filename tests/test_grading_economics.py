"""B9: Tests for grading economics estimator."""
from __future__ import annotations

import pytest
from app import models
from app.services.grading import (
    estimate_grading_economics,
    PSA_MULTIPLIERS,
    PSA_COSTS,
    SGC_COSTS,
    SGC_MULTIPLIERS,
)


def _card(comp_median: float = 0.0, comp_median_weighted: float = None) -> models.Card:
    c = models.Card(
        player="Test Player",
        year=2020,
        set_brand="Topps",
        comp_median=comp_median,
    )
    if comp_median_weighted is not None:
        c.comp_median_weighted = comp_median_weighted
    return c


class TestPSAMultipliers:
    def test_psa_8_multiplier(self):
        assert PSA_MULTIPLIERS[8] == 1.5

    def test_psa_9_multiplier(self):
        assert PSA_MULTIPLIERS[9] == 3.0

    def test_psa_10_multiplier(self):
        assert PSA_MULTIPLIERS[10] == 8.0

    def test_psa_grades_ascending(self):
        grades = sorted(PSA_MULTIPLIERS)
        mults = [PSA_MULTIPLIERS[g] for g in grades]
        # Higher grade = higher multiplier
        for i in range(len(mults) - 1):
            assert mults[i] < mults[i + 1]


class TestPSACosts:
    def test_value_tier(self):
        assert PSA_COSTS["Value"] == 25.0

    def test_regular_tier(self):
        assert PSA_COSTS["Regular"] == 75.0

    def test_express_tier(self):
        assert PSA_COSTS["Express"] == 150.0


class TestSGCCosts:
    def test_standard_tier(self):
        assert SGC_COSTS["Standard"] == 30.0

    def test_economy_tier(self):
        assert SGC_COSTS["Economy"] == 18.0


class TestEstimateGradingEconomics:
    def test_psa9_math(self):
        # raw=100, PSA 9 → 3x = 300, cost=25, net = 300 - 25 - 100 = 175
        card = _card(comp_median=100.0)
        result = estimate_grading_economics(card, "PSA", expected_grade=9, service_level="Value")
        assert result["raw_value"] == 100.0
        assert result["cost"] == 25.0
        assert result["projected_value"] == pytest.approx(300.0)
        assert result["projected_net"] == pytest.approx(175.0)

    def test_psa10_math(self):
        card = _card(comp_median=50.0)
        result = estimate_grading_economics(card, "PSA", expected_grade=10, service_level="Value")
        assert result["projected_value"] == pytest.approx(400.0)  # 50 * 8
        assert result["projected_net"] == pytest.approx(325.0)    # 400 - 25 - 50

    def test_psa8_math(self):
        card = _card(comp_median=40.0)
        result = estimate_grading_economics(card, "PSA", expected_grade=8, service_level="Value")
        assert result["projected_value"] == pytest.approx(60.0)   # 40 * 1.5
        assert result["projected_net"] == pytest.approx(-5.0)     # 60 - 25 - 40

    def test_sgc_service(self):
        card = _card(comp_median=100.0)
        result = estimate_grading_economics(card, "SGC", expected_grade=9, service_level="Standard")
        assert result["cost"] == SGC_COSTS["Standard"]
        assert result["projected_value"] == pytest.approx(100.0 * SGC_MULTIPLIERS[9])

    def test_breakeven_grade_returned(self):
        card = _card(comp_median=100.0)
        result = estimate_grading_economics(card, "PSA", expected_grade=9, service_level="Value")
        # raw=100, cost=25; need raw*mult > 125, so mult > 1.25
        # PSA 7 is 1.25 (not >, would fail), PSA 8 is 1.5 (passes)
        assert result["breakeven_grade"] is not None
        assert result["breakeven_grade"] in PSA_MULTIPLIERS

    def test_breakeven_grade_correct_low_value(self):
        # Very low value card — hard to break even
        card = _card(comp_median=5.0)
        result = estimate_grading_economics(card, "PSA", expected_grade=9, service_level="Value")
        # raw=5, cost=25; need 5*mult > 30 → mult > 6. PSA 10 = 8x passes.
        assert result["breakeven_grade"] == 10

    def test_breakeven_none_when_impossible(self):
        # $1 card, all multipliers can't overcome cost
        card = _card(comp_median=1.0)
        result = estimate_grading_economics(card, "PSA", expected_grade=9, service_level="Regular")
        # cost=75, need 1*mult > 76; no grade achieves this
        assert result["breakeven_grade"] is None

    def test_uses_weighted_median_over_comp_median(self):
        card = _card(comp_median=50.0)
        card.comp_median_weighted = 80.0
        result = estimate_grading_economics(card, "PSA", expected_grade=9, service_level="Value")
        # Should use 80, not 50
        assert result["raw_value"] == pytest.approx(80.0)
        assert result["projected_value"] == pytest.approx(80.0 * 3.0)

    def test_result_keys_present(self):
        card = _card(comp_median=100.0)
        result = estimate_grading_economics(card, "PSA", expected_grade=9)
        for key in ("raw_value", "cost", "projected_value", "projected_net", "breakeven_grade"):
            assert key in result
