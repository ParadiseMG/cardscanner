"""Auto-pair card photos by EXIF capture time.

Heuristic: iPhone Camera in burst-photograph-the-card workflow takes
front-then-back within a few seconds. Sort all images by their EXIF
DateTimeOriginal; consecutive images within `WINDOW_SECONDS` of each
other become a (front, back) pair. Anything else is single-sided.

Falls back gracefully:
- If both timestamps in a candidate pair are missing → singleton.
- If filenames already follow `*_front.jpg` / `*_back.jpg` convention,
  that wins (explicit > inferred).

Both upload paths use this:
- Drag-drop fallback (`pipeline.run_job`) on local files.
- Drive sync (`pipeline._run_drive_job`) after the bytes are downloaded.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable, Optional, Tuple

from PIL import Image

from app.utils import logger as _log
from app.utils.images import read_capture_time

log = _log.get(__name__)

WINDOW_SECONDS = 15

# --- Verification thresholds (tunable) ---
# 8x8 perceptual hash; Hamming distance over 64 bits.
# < 8  : virtually identical → almost certainly the same side photographed twice
# 8-50 : different views of related content → plausible front/back pair
# > 50 : visually unrelated
_PHASH_MIN_DIFFERENCE = 8     # below this, refuse to pair (probably dup of front)
_PHASH_MAX_DIFFERENCE = 50    # above this, refuse to pair (probably unrelated cards)

# A real card-back typically has lower saturation than its front (lots of
# B&W stats text). If both photos have HIGH saturation, they're probably
# both fronts of two different cards.
_SATURATION_MAX_BOTH = 0.45   # both above this → suspect "two fronts"

# Aspect-ratio sanity: same card photographed front+back is the same
# physical object → very similar dimensions. Allow 15% drift for hand-held shots.
_ASPECT_RATIO_TOLERANCE = 0.15

_PAIR_RE = re.compile(r"^(?P<base>.+?)_(?P<side>front|back)\.(?P<ext>[A-Za-z0-9]+)$", re.I)


@dataclass
class Pair:
    """A pairing decision: front (always present) + optional back."""
    front: Path
    back: Optional[Path] = None


def auto_pair(paths: list[Path]) -> list[Pair]:
    """Group a flat list of image paths into (front, back?) pairs.

    Order of operations:
    1. Pull out filename-suffix pairs (`*_front.jpg` + `*_back.jpg`) — explicit
       intent always wins.
    2. For everything else, read EXIF capture times and sort.
    3. Walk the sorted list pairing consecutive images within WINDOW_SECONDS.
    4. Anything not paired becomes single-sided.
    """
    if not paths:
        return []

    # Step 1: filename-suffix pairs
    explicit: list[Pair] = []
    by_base: dict[str, dict[str, Path]] = {}
    leftover: list[Path] = []
    for p in paths:
        m = _PAIR_RE.match(p.name)
        if m:
            base = m.group("base").lower()
            side = m.group("side").lower()
            by_base.setdefault(base, {})[side] = p
        else:
            leftover.append(p)
    for _base, sides in by_base.items():
        front = sides.get("front")
        back = sides.get("back")
        if front:
            explicit.append(Pair(front=front, back=back))
        elif back:
            # Only the back was named — process it alone as the front
            explicit.append(Pair(front=back))

    # Step 2/3: time-based pairing on the rest
    timed = [(read_capture_time(p), p) for p in leftover]
    # None timestamps go last so they stay singletons
    timed.sort(key=lambda x: (x[0] is None, x[0] or datetime.min))

    inferred: list[Pair] = []
    i = 0
    n = len(timed)
    while i < n:
        t1, p1 = timed[i]
        if i + 1 < n:
            t2, p2 = timed[i + 1]
            if t1 and t2 and (t2 - t1) <= timedelta(seconds=WINDOW_SECONDS):
                ok, reason = verify_pair(p1, p2)
                if ok:
                    inferred.append(Pair(front=p1, back=p2))
                    log.info("auto_pair confirmed",
                             extra={"front": p1.name, "back": p2.name,
                                    "delta_s": int((t2 - t1).total_seconds())})
                    i += 2
                    continue
                else:
                    log.info("auto_pair rejected",
                             extra={"a": p1.name, "b": p2.name,
                                    "delta_s": int((t2 - t1).total_seconds()),
                                    "reason": reason})
                    # Fall through: treat p1 as singleton, advance to p2 next loop
        inferred.append(Pair(front=p1))
        i += 1

    return explicit + inferred


# ---------------------------------------------------------------------------
# Verification — does this candidate pair actually look like front+back of
# one card, or did Connor just photograph two different cards in quick
# succession?
# ---------------------------------------------------------------------------
def verify_pair(a: Path, b: Path) -> Tuple[bool, str]:
    """Three cheap visual checks. All three must pass to confirm the pair.

    Returns (ok, reason). On any error reading either image, returns
    (False, "read_error") so we stay conservative — false positives here
    cost real money (mis-attributed listings).
    """
    try:
        with Image.open(a) as ima, Image.open(b) as imb:
            # 1. Aspect-ratio match (same physical card)
            ar_a = ima.size[0] / ima.size[1] if ima.size[1] else 0
            ar_b = imb.size[0] / imb.size[1] if imb.size[1] else 0
            if ar_a == 0 or ar_b == 0:
                return False, "zero_dim"
            # Compare in a rotation-agnostic way (long/short ratio)
            r_a = max(ar_a, 1 / ar_a)
            r_b = max(ar_b, 1 / ar_b)
            if abs(r_a - r_b) / max(r_a, r_b) > _ASPECT_RATIO_TOLERANCE:
                return False, f"aspect_mismatch ({r_a:.2f} vs {r_b:.2f})"

            # 2. Perceptual-hash distance: must be different but related
            ha = _phash(ima)
            hb = _phash(imb)
            dist = _hamming(ha, hb)
            if dist < _PHASH_MIN_DIFFERENCE:
                return False, f"too_similar (phash dist={dist}; likely duplicate)"
            if dist > _PHASH_MAX_DIFFERENCE:
                return False, f"too_different (phash dist={dist}; unrelated)"

            # 3. Saturation: backs are usually less colorful. If BOTH photos
            #    are vibrant, they're probably both fronts of different cards.
            sat_a = _mean_saturation(ima)
            sat_b = _mean_saturation(imb)
            if sat_a > _SATURATION_MAX_BOTH and sat_b > _SATURATION_MAX_BOTH:
                return False, f"both_vibrant (sat={sat_a:.2f}, {sat_b:.2f}; likely two fronts)"

        return True, "ok"
    except Exception as e:
        return False, f"read_error: {type(e).__name__}"


def _phash(im: Image.Image) -> int:
    """8x8 average-hash → 64-bit int. Cheap and good enough for "same scene?"."""
    g = im.convert("L").resize((8, 8), Image.LANCZOS)
    pixels = list(g.getdata())
    avg = sum(pixels) / 64
    bits = 0
    for i, p in enumerate(pixels):
        if p > avg:
            bits |= (1 << i)
    return bits


def _hamming(h1: int, h2: int) -> int:
    return bin(h1 ^ h2).count("1")


def _mean_saturation(im: Image.Image) -> float:
    """Mean S channel of HSV thumbnail, normalized 0..1."""
    hsv = im.convert("RGB").resize((64, 64), Image.LANCZOS).convert("HSV")
    s_band = hsv.getdata(band=1)
    return (sum(s_band) / len(s_band)) / 255.0
