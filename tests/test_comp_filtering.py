"""B6: Tests for filter_outliers, detect_suspicious, recency_weighted."""
from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional

import pytest

from app.services import comp_lookup


# ---------------------------------------------------------------------------
# filter_outliers
# ---------------------------------------------------------------------------
class TestFilterOutliers:
    def test_short_list_unchanged(self):
        prices = [1.0, 2.0, 3.0]
        assert comp_lookup.filter_outliers_for_test(prices) == prices

    def test_exactly_nine_unchanged(self):
        prices = list(range(1, 10))  # 9 items
        assert comp_lookup.filter_outliers_for_test(prices) == prices

    def test_ten_trims_one_each_end(self):
        prices = [1.0] + [5.0] * 8 + [100.0]  # outliers at each end
        result = comp_lookup.filter_outliers_for_test(prices)
        # Should drop the 1.0 and 100.0
        assert 1.0 not in result
        assert 100.0 not in result
        assert all(p == 5.0 for p in result)

    def test_twenty_trims_two_each_end(self):
        prices = [0.1, 0.2] + [10.0] * 16 + [200.0, 300.0]
        result = comp_lookup.filter_outliers_for_test(prices)
        assert min(result) >= 10.0
        assert max(result) <= 10.0

    def test_empty_list(self):
        assert comp_lookup.filter_outliers_for_test([]) == []

    def test_returns_sorted_slice(self):
        # Result should be a sorted subset
        prices = [float(i) for i in range(10, 30)]  # 20 items
        result = comp_lookup.filter_outliers_for_test(prices)
        assert result == sorted(result)
        assert len(result) == 16  # 20 - 2*2


# ---------------------------------------------------------------------------
# detect_suspicious
# ---------------------------------------------------------------------------
class TestDetectSuspicious:
    def test_not_suspicious_diverse_pricing(self):
        prices = [1.0, 5.0, 10.0, 50.0, 100.0, 200.0]
        susp, reason = comp_lookup.detect_suspicious_for_test(prices)
        assert susp is False
        assert reason == ""

    def test_not_suspicious_fewer_than_five(self):
        prices = [10.0, 10.01, 10.02, 10.03]
        susp, reason = comp_lookup.detect_suspicious_for_test(prices)
        assert susp is False

    def test_suspicious_five_identical(self):
        prices = [10.0] * 5 + [1.0, 200.0]
        susp, reason = comp_lookup.detect_suspicious_for_test(prices)
        assert susp is True
        assert "5" in reason
        assert "10.00" in reason
        assert "fingerprint" in reason.lower()

    def test_suspicious_within_one_percent(self):
        base = 20.0
        prices = [base * (1 + i * 0.005) for i in range(6)]  # within 0.5% each
        susp, reason = comp_lookup.detect_suspicious_for_test(prices)
        assert susp is True

    def test_suspicious_exact_count_in_reason(self):
        prices = [10.0] * 8 + [50.0, 75.0]
        susp, reason = comp_lookup.detect_suspicious_for_test(prices)
        assert susp is True
        assert "8" in reason

    def test_not_suspicious_empty(self):
        susp, reason = comp_lookup.detect_suspicious_for_test([])
        assert susp is False
        assert reason == ""


# ---------------------------------------------------------------------------
# recency_weighted
# ---------------------------------------------------------------------------
class TestRecencyWeighted:
    def _dt(self, days_ago: int) -> datetime:
        return datetime.now(timezone.utc) - timedelta(days=days_ago)

    def test_empty_returns_none(self):
        assert comp_lookup.recency_weighted_for_test([]) is None

    def test_missing_date_falls_back_to_plain_median(self):
        samples = [(10.0, None), (20.0, None), (30.0, None)]
        result = comp_lookup.recency_weighted_for_test(samples)
        assert result == 20.0  # plain median

    def test_recent_prices_weighted_higher(self):
        # Old sample at $1, very recent at $100 — weighted median should skew toward $100
        samples = [
            (1.0, self._dt(60)),   # weight 1
            (100.0, self._dt(1)),  # weight 3
        ]
        result = comp_lookup.recency_weighted_for_test(samples)
        # Weighted list: [1.0, 100.0, 100.0, 100.0] → median = 100.0
        assert result == 100.0

    def test_last_30_days_gets_weight_2(self):
        samples = [
            (1.0, self._dt(60)),   # weight 1 → [1.0]
            (50.0, self._dt(15)),  # weight 2 → [50.0, 50.0]
        ]
        result = comp_lookup.recency_weighted_for_test(samples)
        # Weighted: [1.0, 50.0, 50.0] → median = 50.0
        assert result == 50.0

    def test_all_old_weight_one(self):
        samples = [(10.0, self._dt(90)), (20.0, self._dt(90)), (30.0, self._dt(90))]
        result = comp_lookup.recency_weighted_for_test(samples)
        assert result == 20.0  # plain-ish median (all weight 1)

    def test_single_sample(self):
        samples = [(42.5, self._dt(1))]
        assert comp_lookup.recency_weighted_for_test(samples) == 42.5

    def test_mixed_none_falls_back(self):
        """One missing date should trigger fallback to plain median."""
        samples = [(10.0, self._dt(1)), (20.0, None), (30.0, self._dt(5))]
        result = comp_lookup.recency_weighted_for_test(samples)
        assert result == 20.0  # plain median

    def test_naive_datetime_handled(self):
        """Naive datetimes (no tzinfo) should not raise."""
        naive_dt = datetime.utcnow() - timedelta(days=3)
        samples = [(15.0, naive_dt), (25.0, naive_dt)]
        result = comp_lookup.recency_weighted_for_test(samples)
        assert result is not None
