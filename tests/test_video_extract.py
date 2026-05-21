"""Tests for video frame extraction logic."""
import numpy as np
import pytest
import cv2
from pathlib import Path
from unittest.mock import patch, MagicMock

from app.services.video_extract import (
    sharpness_score,
    find_still_windows,
    pick_best_frame,
)


def test_sharpness_score_sharp_image():
    """A sharp image (high-frequency edges) scores higher than a blurry one."""
    sharp = np.zeros((100, 100), dtype=np.uint8)
    sharp[::2, :] = 255  # alternating black/white rows = very sharp
    blurry = cv2.GaussianBlur(sharp, (21, 21), 10)
    assert sharpness_score(sharp) > sharpness_score(blurry)


def test_find_still_windows_detects_pauses():
    """Motion scores with clear dips should produce still windows."""
    scores = (
        [500.0] * 5    # motion
        + [10.0] * 15  # still window 1
        + [500.0] * 5  # motion
        + [10.0] * 15  # still window 2
        + [500.0] * 5  # motion
    )
    windows = find_still_windows(scores, threshold=50.0, min_frames=10)
    assert len(windows) == 2
    assert windows[0] == (5, 19)
    assert windows[1] == (25, 39)


def test_find_still_windows_no_pauses():
    """All-motion input should return no windows."""
    scores = [500.0] * 30
    windows = find_still_windows(scores, threshold=50.0, min_frames=10)
    assert len(windows) == 0


def test_find_still_windows_too_short():
    """A pause shorter than min_frames should be ignored."""
    scores = [500.0] * 5 + [10.0] * 5 + [500.0] * 5
    windows = find_still_windows(scores, threshold=50.0, min_frames=10)
    assert len(windows) == 0


def test_pick_best_frame_returns_sharpest():
    """From a list of frames, pick_best_frame returns the sharpest."""
    blurry = np.random.randint(0, 50, (100, 100, 3), dtype=np.uint8)
    sharp = np.zeros((100, 100, 3), dtype=np.uint8)
    sharp[::2, :] = 255
    medium = cv2.GaussianBlur(sharp, (5, 5), 1)
    frames = [blurry, medium, sharp]
    best_idx = pick_best_frame(frames)
    assert best_idx == 2  # sharp is the last one
