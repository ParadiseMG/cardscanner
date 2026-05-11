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
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Optional

import httpx

from app.config import settings
from app.utils import logger as _log
from app.utils.images import normalize as normalize_image
from app.utils.retry import with_backoff

ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
TIMEOUT_SECONDS = 30.0
RETRY_ATTEMPTS = 3

# Fields eligible for re-prompt (lowest to highest priority in re-prompt order)
_REPROMPT_FIELDS = ("parallel", "card_no")
_REPROMPT_THRESHOLD = 0.5
_REPROMPT_MAX_FIELDS = 2

log = _log.get(__name__)


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
    # A1: enriched fields
    is_rookie: bool = False
    is_serial_numbered: bool = False
    serial_print_run: Optional[int] = None      # e.g. 50 from "/50"
    field_confidence: dict[str, float] = field(default_factory=dict)
    condition_signals: dict[str, str] = field(default_factory=dict)
    low_confidence_fields: list[str] = field(default_factory=list)
    photo_quality: Optional[str] = None         # "good"/"blurry"/"obstructed"/"off_angle"

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
  "notes": <string or null, anything noteworthy>,
  "is_rookie": <bool, true if this is a recognized rookie card>,
  "is_serial_numbered": <bool, true if card has a printed serial number e.g. "/50">,
  "serial_print_run": <int or null, the print run number e.g. 50 for "/50">,
  "field_confidence": {
    "year": <float 0..1>,
    "set_brand": <float 0..1>,
    "player": <float 0..1>,
    "card_no": <float 0..1>,
    "parallel": <float 0..1>,
    "condition": <float 0..1>
  },
  "condition_signals": {
    "centering": <string or null e.g. "55/45">,
    "corners": <string or null e.g. "sharp" / "light wear" / "rounded">,
    "edges": <string or null e.g. "clean" / "rough" / "chipping">,
    "surface": <string or null e.g. "clean" / "scratches" / "print defects">
  },
  "photo_quality": <string: "good" / "blurry" / "obstructed" / "off_angle">
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
- field_confidence: rate each key field 0..1 (1.0 = certain, 0.0 = guessing).
  Use lower values when the photo is blurry, text is obscured, or you are
  making an educated guess.
- condition_signals: omit keys where not visible (set to null).
- photo_quality: "good" for clear, well-lit, front-facing shots. "blurry" for
  out-of-focus. "obstructed" for fingers/glare blocking parts. "off_angle" for
  heavily skewed/angled shots.
- is_rookie: true only for recognized first-year or prospect RC cards.
- is_serial_numbered: true only when you can see a stamped serial number (e.g.
  "47/100"). If true, set serial_print_run to the total print run (100 in that
  example).
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


def _build_reprompt_content(
    fp: Path,
    bp: Optional[Path],
    field_name: str,
    current_value,
) -> list[dict]:
    """Build the content list for a focused re-prompt on a single field."""
    content: list[dict] = [
        {"type": "text", "text": "Front of card:"},
        _b64_image(str(fp)),
    ]
    if bp:
        content.append({"type": "text", "text": "Back of card:"})
        content.append(_b64_image(str(bp)))
    prompt_text = (
        f"Look very carefully at this baseball card image.\n\n"
        f"I need you to identify ONLY the '{field_name}' field.\n"
        f"Current value I have is: {current_value!r}\n\n"
        f"Return STRICT JSON ONLY with exactly two keys, no prose, no markdown:\n"
        f'{{"value": <the correct value or null>, "confidence": <float 0..1>}}'
    )
    content.append({"type": "text", "text": prompt_text})
    return content


async def _do_reprompt(
    fp: Path,
    bp: Optional[Path],
    field_name: str,
    current_value,
    key: str,
    client: httpx.AsyncClient,
) -> tuple[any, float]:
    """Issue a focused follow-up call for a single field.

    Returns (new_value, new_confidence). On any error, returns the original
    value with confidence 0.0 (caller keeps original and logs a warning).
    """
    content = _build_reprompt_content(fp, bp, field_name, current_value)
    payload = {
        "model": settings.anthropic_model,
        "max_tokens": 100,
        "messages": [{"role": "user", "content": content}],
    }
    headers = {
        "x-api-key": key,
        "anthropic-version": ANTHROPIC_VERSION,
        "content-type": "application/json",
    }
    try:
        resp = await client.post(ANTHROPIC_URL, json=payload, headers=headers,
                                 timeout=TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()
        text_blocks = [
            b.get("text", "") for b in data.get("content", [])
            if b.get("type") == "text"
        ]
        raw = "\n".join(text_blocks)
        parsed = _parse_json_block(raw)
        new_value = parsed.get("value", current_value)
        new_conf = float(parsed.get("confidence", 0.0) or 0.0)
        return new_value, new_conf
    except Exception as exc:
        log.warning("vision_reprompt junk response",
                    extra={"field": field_name, "error": str(exc)})
        return current_value, 0.0


async def identify_card_async(
    front_path: str,
    back_path: Optional[str] = None,
    api_key_override: Optional[str] = None,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> CardIdentification:
    key = _key_or_raise(api_key_override)

    # Normalize HEIC / large / mis-rotated images before sending to Anthropic.
    fp = normalize_image(Path(front_path))
    bp = normalize_image(Path(back_path)) if back_path else None

    content: list[dict] = [
        {"type": "text", "text": "Front of card:"},
        _b64_image(str(fp)),
    ]
    if bp:
        content.append({"type": "text", "text": "Back of card:"})
        content.append(_b64_image(str(bp)))
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
        client = httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
    try:
        async def _call() -> httpx.Response:
            r = await client.post(ANTHROPIC_URL, json=payload, headers=headers,
                                  timeout=TIMEOUT_SECONDS)
            r.raise_for_status()
            return r

        resp = await with_backoff(
            _call, attempts=RETRY_ATTEMPTS, base_delay=1.5,
            on_retry=lambda n, e: log.warning(
                "claude retry", extra={"attempt": n, "error": type(e).__name__}),
        )
        data = resp.json()
    finally:
        if own_client:
            await client.aclose()

    text_blocks = [b.get("text", "") for b in data.get("content", []) if b.get("type") == "text"]
    raw = "\n".join(text_blocks)
    parsed = _parse_json_block(raw)

    # Parse field_confidence (tolerate missing/malformed)
    raw_fc = parsed.get("field_confidence") or {}
    field_confidence: dict[str, float] = {}
    if isinstance(raw_fc, dict):
        for k, v in raw_fc.items():
            try:
                field_confidence[k] = float(v)
            except (TypeError, ValueError):
                field_confidence[k] = 0.0

    # Parse condition_signals (tolerate missing keys)
    raw_cs = parsed.get("condition_signals") or {}
    condition_signals: dict[str, str] = {}
    if isinstance(raw_cs, dict):
        for k, v in raw_cs.items():
            if v is not None:
                condition_signals[k] = str(v)

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
        is_rookie=bool(parsed.get("is_rookie", False)),
        is_serial_numbered=bool(parsed.get("is_serial_numbered", False)),
        serial_print_run=_safe_int(parsed.get("serial_print_run")),
        field_confidence=field_confidence,
        condition_signals=condition_signals,
        photo_quality=parsed.get("photo_quality"),
    )

    # A2: low-confidence re-prompt for parallel / card_no
    # Use a fresh short-lived client for re-prompts (the outer client may be
    # closed by the time we get here if caller passed own_client=True).
    reprompt_count = 0
    reprompt_client_own = False
    reprompt_client: Optional[httpx.AsyncClient] = None

    try:
        for field_name in _REPROMPT_FIELDS:
            if reprompt_count >= _REPROMPT_MAX_FIELDS:
                break
            conf = field_confidence.get(field_name, 1.0)
            if conf >= _REPROMPT_THRESHOLD:
                continue

            # Need a live client for re-prompts
            if reprompt_client is None:
                reprompt_client = httpx.AsyncClient(timeout=TIMEOUT_SECONDS)
                reprompt_client_own = True

            current_value = getattr(cid, field_name, None)
            with _log.step(log, "vision_reprompt", field=field_name):
                new_value, new_conf = await _do_reprompt(
                    fp, bp, field_name, current_value, key, reprompt_client
                )

            # Only adopt the new value if the re-prompt succeeded meaningfully
            if new_conf > conf or new_value != current_value:
                setattr(cid, field_name, new_value)
                field_confidence[field_name] = new_conf
            reprompt_count += 1
    finally:
        if reprompt_client_own and reprompt_client is not None:
            await reprompt_client.aclose()

    # Recompute field_confidence on cid (may have been updated by re-prompts)
    cid.field_confidence = field_confidence

    # Compute low_confidence_fields after any re-prompts
    cid.low_confidence_fields = [
        f for f in ("year", "set_brand", "player", "card_no", "parallel", "condition")
        if field_confidence.get(f, 1.0) < _REPROMPT_THRESHOLD
    ]

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
