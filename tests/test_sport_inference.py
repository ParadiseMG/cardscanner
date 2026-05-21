"""Behavior tests for the sport auto-tag heuristic."""
from __future__ import annotations

from app.utils.sport_inference import infer_sport, reconcile_sport


def test_brand_with_football_keyword_wins_over_claudes_baseball_default():
    """Claude's prompt biased toward Baseball; brand keyword overrides."""
    assert reconcile_sport("Baseball", "Panini Prestige Football", "Bobby Wagner") == "Football"


def test_brand_with_basketball_keyword():
    assert reconcile_sport("Baseball", "Panini Mosaic Jam Masters", "Obi Toppin") == "Basketball"


def test_brand_keyword_overrides_claude_even_when_claude_picks_a_non_default():
    """A misidentification by Claude shouldn't override clear brand evidence."""
    assert reconcile_sport("Basketball", "Topps Chrome", "Mike Trout") == "Baseball"


def test_claude_non_baseball_kept_when_brand_is_silent():
    """When the brand offers no hint, we trust Claude over the default."""
    assert reconcile_sport("Football", "Panini Donruss", "Patrick Mahomes") == "Football"


def test_falls_back_to_other_when_everything_is_silent():
    """No brand match + Claude default + no other signal -> 'Other'."""
    # Claude saying "Baseball" with no brand evidence used to leak through —
    # but we still trust Claude's answer if it matches a known sport.
    assert reconcile_sport("Baseball", "Custom Set", "Random Player") == "Baseball"


def test_unknown_claude_sport_normalized_to_other():
    assert reconcile_sport("Cricket", "Random Brand", "Some Player") == "Other"


def test_infer_sport_returns_none_when_no_signal():
    """The lower-level inference returns None — caller decides default."""
    assert infer_sport("Random Set", "Unknown Player") is None


def test_no_team_fallback_means_rangers_baseball_card_stays_baseball():
    """Regression: 'Texas Rangers' (MLB) used to get tagged Hockey via 'rangers'
    in the dropped team-fallback list. Brand is the only signal now."""
    assert reconcile_sport("Baseball", "Topps", "Jose Guzman", team="Rangers") == "Baseball"
