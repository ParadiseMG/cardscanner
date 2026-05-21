"""Pair extracted video frames as front/back card images.

Uses sequential ordering (front, back, front, back, ...) with
vision-based verification to catch mismatches.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class PairedCard:
    front: Path
    back: Optional[Path]
    flagged: bool  # True if pairing is uncertain and needs review


def pair_frames(frames: list[Path], sides: list[str]) -> list[PairedCard]:
    """Pair frames using sequential order + side verification.

    Args:
        frames: Extracted frame paths in video order.
        sides: "front" or "back" for each frame (from vision).

    Returns:
        List of PairedCard, one per card detected.
    """
    pairs: list[PairedCard] = []
    i = 0

    while i < len(frames):
        if i + 1 >= len(frames):
            # Odd frame out — incomplete card
            pairs.append(PairedCard(front=frames[i], back=None, flagged=True))
            i += 1
            continue

        side_a = sides[i]
        side_b = sides[i + 1]

        if side_a == "front" and side_b == "back":
            # Perfect pair
            pairs.append(PairedCard(front=frames[i], back=frames[i + 1], flagged=False))
        elif side_a == "back" and side_b == "front":
            # Reversed — swap and flag
            pairs.append(PairedCard(front=frames[i + 1], back=frames[i], flagged=True))
            logger.warning("Frames %d-%d: back before front, swapped and flagged", i, i + 1)
        else:
            # Same side twice — flag both
            if side_a == "front":
                pairs.append(PairedCard(front=frames[i], back=frames[i + 1], flagged=True))
            else:
                pairs.append(PairedCard(front=frames[i + 1], back=frames[i], flagged=True))
            logger.warning("Frames %d-%d: both %s, flagged for review", i, i + 1, side_a)

        i += 2

    return pairs


async def classify_sides(frames: list[Path]) -> list[str]:
    """Ask vision model whether each frame is a card front or back.

    Uses Ollama for cheap local inference.
    """
    import base64

    sides: list[str] = []
    url = f"{settings.ollama_base_url.rstrip('/')}/api/generate"

    async with httpx.AsyncClient(timeout=30.0) as client:
        for frame_path in frames:
            image_b64 = base64.b64encode(frame_path.read_bytes()).decode()

            resp = await client.post(url, json={
                "model": settings.ollama_vision_model,
                "prompt": (
                    "Is this the front or back of a trading card? "
                    "Reply with exactly one word: front or back"
                ),
                "images": [image_b64],
                "stream": False,
            })

            if resp.status_code == 200:
                answer = resp.json().get("response", "").strip().lower()
                if "front" in answer:
                    sides.append("front")
                elif "back" in answer:
                    sides.append("back")
                else:
                    # Default to expected alternating pattern
                    expected = "front" if len(sides) % 2 == 0 else "back"
                    sides.append(expected)
                    logger.warning(
                        "Unclear side classification for %s: %r, defaulting to %s",
                        frame_path.name, answer, expected,
                    )
            else:
                expected = "front" if len(sides) % 2 == 0 else "back"
                sides.append(expected)
                logger.warning("Vision call failed for %s, defaulting to %s", frame_path.name, expected)

    return sides
