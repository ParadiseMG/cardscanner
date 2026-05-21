"""Heuristic sport inference for trading cards.

Claude's vision response defaults to "Baseball" when it isn't sure, which
mis-tags a lot of multi-sport collections. This module gives us a deterministic
override: scan the printed `set_brand` (the strongest sport signal — most modern
brands explicitly name the sport, e.g. "Panini Prestige Football", "Topps Now
NHL") and fall back to keyword-matching on the team / player name.

If nothing matches, we return None — the caller decides whether to keep Claude's
answer or fall back to a default.
"""
from __future__ import annotations

from typing import Optional

# Canonical sport labels — used everywhere downstream (facets, filters, UI).
SPORTS = ("Baseball", "Football", "Basketball", "Hockey", "Soccer", "Other")

# Set-brand substrings that are dead giveaways. Order matters when a brand
# could plausibly match more than one bucket (Panini Score is football-only
# in practice — but it's not in the map; we'd rather miss than mis-tag).
_BRAND_PATTERNS = {
    "Football":   ("football", "nfl", "draft picks", "rookies & stars",
                   "score football", "panini score"),
    "Basketball": ("basketball", "nba", "hoops", "jam masters",
                   "court kings", "chronicles essentials"),
    "Hockey":     ("hockey", "nhl", "upper deck series", "young guns"),
    "Soccer":     ("soccer", "futbol", "fifa", "mls"),
    "Baseball":   ("baseball", "mlb", "topps chrome", "bowman", "stadium club",
                   "diamond kings", "donruss baseball", "fleer tradition"),
}

# Team-name patterns were too ambiguous to be useful — "Rangers" lives in MLB,
# NHL, *and* NFL (CFL); "Giants" in MLB and NFL. Mis-tagging baseball cards as
# Hockey is worse than leaving them at the brand-default. Skip team fallback.
# (Kept as a name for documentation only.)
_TEAM_PATTERNS: dict[str, tuple] = {}


def infer_sport(set_brand: Optional[str],
                player: Optional[str] = None,
                team: Optional[str] = None) -> Optional[str]:
    """Return a canonical sport label, or None when the inputs give no signal."""
    text_primary = (set_brand or "").lower()
    for sport, needles in _BRAND_PATTERNS.items():
        if any(n in text_primary for n in needles):
            return sport

    text_fallback = " ".join(filter(None, [team or "", player or ""])).lower()
    for sport, needles in _TEAM_PATTERNS.items():
        if any(n in text_fallback for n in needles):
            return sport

    return None


def reconcile_sport(claude_sport: Optional[str],
                    set_brand: Optional[str],
                    player: Optional[str] = None,
                    team: Optional[str] = None) -> str:
    """Combine Claude's answer with the heuristic. Heuristic wins when it has
    a confident answer that contradicts Claude's default-Baseball bias.

    Order of preference:
      1. Heuristic match on set_brand (strongest signal — the brand printed on
         the card front almost always names the sport).
      2. Claude's answer if it isn't the default "Baseball".
      3. Heuristic team/player fallback.
      4. Claude's answer (which may be the default).
      5. "Other" as a last resort.
    """
    brand_hit = None
    text_primary = (set_brand or "").lower()
    for sport, needles in _BRAND_PATTERNS.items():
        if any(n in text_primary for n in needles):
            brand_hit = sport
            break
    if brand_hit:
        return brand_hit

    if claude_sport and claude_sport != "Baseball":
        # Claude actively picked a non-default — trust it.
        return claude_sport if claude_sport in SPORTS else "Other"

    team_hit = None
    text_fallback = " ".join(filter(None, [team or "", player or ""])).lower()
    for sport, needles in _TEAM_PATTERNS.items():
        if any(n in text_fallback for n in needles):
            team_hit = sport
            break
    if team_hit:
        return team_hit

    if claude_sport in SPORTS:
        return claude_sport
    return "Other"
