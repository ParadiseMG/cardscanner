"""Tests for video frame pairing logic."""
import pytest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.services.video_pair import pair_frames, PairedCard


def test_pair_frames_even_count():
    """Even number of frames pairs sequentially."""
    frames = [Path(f"frame_{i}.jpg") for i in range(6)]
    sides = ["front", "back", "front", "back", "front", "back"]
    pairs = pair_frames(frames, sides)
    assert len(pairs) == 3
    assert pairs[0] == PairedCard(front=frames[0], back=frames[1], flagged=False)
    assert pairs[1] == PairedCard(front=frames[2], back=frames[3], flagged=False)
    assert pairs[2] == PairedCard(front=frames[4], back=frames[5], flagged=False)


def test_pair_frames_odd_count():
    """Odd number flags last card as incomplete."""
    frames = [Path(f"frame_{i}.jpg") for i in range(5)]
    sides = ["front", "back", "front", "back", "front"]
    pairs = pair_frames(frames, sides)
    assert len(pairs) == 3
    assert pairs[2] == PairedCard(front=frames[4], back=None, flagged=True)


def test_pair_frames_consecutive_same_side():
    """Two fronts in a row flags the pair for review."""
    frames = [Path(f"frame_{i}.jpg") for i in range(4)]
    sides = ["front", "front", "back", "back"]
    pairs = pair_frames(frames, sides)
    assert len(pairs) == 2
    assert pairs[0].flagged is True
    assert pairs[1].flagged is True


def test_pair_frames_back_then_front():
    """Back-first pair still works but flags."""
    frames = [Path("a.jpg"), Path("b.jpg")]
    sides = ["back", "front"]
    pairs = pair_frames(frames, sides)
    assert len(pairs) == 1
    assert pairs[0].front == frames[1]
    assert pairs[0].back == frames[0]
    assert pairs[0].flagged is True
