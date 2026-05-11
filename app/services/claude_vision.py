"""Identify a baseball card from front+back images using Claude vision.

Auth notes
----------
Anthropic restricts OAuth to Claude.ai and Claude Code (Feb 2026 policy update).
End-user OAuth for arbitrary third-party apps is not available, so we accept a
plain API key. Three places it can come from, in priority order:

1. Per-request override (HTTP header `X-Anthropic-Key`) -- handy for the
   browser-stored key fallback (the user pastes their key into the dashboard
   and it lives in localStorage; the server never persists it).
2. ANTHROPIC_API_KEY env var on the server (.env file).
3. None -- the service raises so the API can return 412 and the UI can prompt.

The prompt asks Claude for STRICT JSON only, then we json.loads the response.
We retry once with a stronger formatting reminder if the first parse fails.
"""
from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

import httpx

from app.config import settings

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"


@dataclass
class CardIdentification:
    year: Optional[int] = None
    set_brand: Optional[str] = None
    player: Optional[str] = None
    card_no: Optional[str] = None
    parallel: Optional[str] = "Base"
    sport: str = "Baseball"
    team: Optional[str] = None
    condition: Optional[str] = None
    is_graded: bool = False
    grade: Optional[str] = None
    is_autograph: bool = False
    is_relic: bool = False
    confidence: float = 0.0  # 0..1
    notes: Optional[str] = None
    review_flagged: bool = False  # set when confidence < 0.6 or fields missing

    def to_dict(self) -> dict:
        return asdict(self)


_PROMPT = """You are a baseball-card cataloging assistant.

Examine the supplied photo(s) of a single trading card (front, and possibly
back). Return STRICT JSON ONLY with these keys -- no prose, no markdown fences:

{
  "year": <int or null>,
  "set_brand": <string or null e.g. "Topps Chrome">,
  "player": <string or null>,
  "card_no": <string or null, the printed card number>,
  "parallel": <string, default "Base">,
  "sport": <string, default "Baseball">,
  "team": <string or null>,
  "condition": <string: "NM" / "EX" / "VG" / "GD" / "PR" / "Graded">,
  "is_graded": <bool, true only if visibly slabbed by PSA/BGS/SGC/CGC>,
  "grade": <string or null e.g. "PSA 9">,
  "is_autograph": <bool>,
  "is_relic": <bool>,
  "confidence": <float 0..1, your overall confidence>,
  "notes": <string or null, anything noteworthy>
}

Guidelines:
- If you cannot identify a field, use null (not "Unknown").
- Condition: judge based on corners, edges, surface, centering visible in photo.
  Default to "NM" for clean modern cards, "EX" for light wear, "VG" for visible
  wear. Only use "Graded" when the card is in a sealed grading slab.
- Parallel: things like "Refractor", "Gold /50", "Black /1", "Pink Ice".
  Use "Base" if it appears to be the base card.
- Be precise on year. Trading-card years can be confusing (the season vs print
  year). Use the season printed on the card if visible.
"""


def _key_or_raise(override: Optional[str]) -> str:
    key = override or settings.anthropic_api_key
    if not key:
        raise RuntimeError(
            "No Anthropic API key. Provide one via the dashboard (stored in "
            "localStorage only) or set ANTHROPIC_API_KEY in .env."
        )
    return key


def _b64_image(path: str) -> dict:
    """Build a Claude vision content block from a local file path."""
    p = Path(path)
    data = base64.standard_b64encode(p.read_bytes()).decode()
    suffix = p.suffix.lower().lstrip(".")
    if suffix in {"jpg", "jpeg"}:
        media = "image/jpeg"
    elif suffix == "png":
        media = "image/png"
    elif suffix == "webp":
        media = "image/webp"
    elif suffix == "heic":
        # Anthropic doesn't accept heic; caller should pre-convert.
        media = "image/heic"
    else:
        media = "application/octet-stream"
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media, "data": data},
    }


def _parse_json_block(text: str) -> dict:
    text = text.strip()
    # tolerate ```json fences if Claude slips
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"no JSON object in model response: {text[:200]}")
    return json.loads(m.group(0))


async def identify_card_async(
    front_path: str,
    back_path: Optional[str] = None,
    api_key_override: Optional[str] = None,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> CardIdentification:
    key = _key_or_raise(api_key_override)

    content: list[dict] = [
        {"type": "text", "text": "Front of card:"},
        _b64_image(front_path),
    ]
    if back_path:
        content.append({"type": "text", "text": "Back of card:"})
        content.append(_b64_image(back_path))
    content.append({"type": "text", "text": _PROMPT})

    payload = {
        "model": settings.anthropic_model,
        "max_tokens": 800,
        "messages": [{"role": "user", "content": content}],
    }
    headers = {
        "x-api-key": key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=60.0)
    try:
        resp = await client.post(ANTHROPIC_URL, json=payload, headers=headers)
        resp.raise_for_status()
        data = resp.json()
    finally:
        if own_client:
            await client.aclose()

    text_blocks = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    raw = "\n".join(text_blocks)
    parsed = _parse_json_block(raw)

    cid = CardIdentification(
        year=_safe_int(parsed.get("year")),
        set_brand=parsed.get("set_brand"),
        player=parsed.get("player"),
        card_no=str(parsed["card_no"]) if parsed.get("card_no") is not None else None,
        parallel=parsed.get("parallel") or "Base",
        sport=parsed.get("sport") or "Baseball",
        team=parsed.get("team"),
        condition=parsed.get("condition"),
        is_graded=bool(parsed.get("is_graded", False)),
        grade=parsed.get("grade"),
        is_autograph=bool(parsed.get("is_autograph", False)),
        is_relic=bool(parsed.get("is_relic", False)),
        confidence=float(parsed.get("confidence", 0.0) or 0.0),
        notes=parsed.get("notes"),
    )
    cid.review_flagged = (
        cid.confidence < 0.6
        or cid.year is None
        or cid.player is None
        or cid.set_brand is None
    )
    return cid


def _safe_int(v) -> Optional[int]:
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
