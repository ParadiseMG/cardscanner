"""Video frame extraction for card scanning.

Takes a video of cards being shown (front, flip, back, next card, ...)
and extracts the clearest frame from each still moment.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from app.utils.images import normalize

logger = logging.getLogger(__name__)

# --- Low-level helpers (tested directly) ---

def sharpness_score(gray: np.ndarray) -> float:
    """Laplacian variance — higher means sharper."""
    return cv2.Laplacian(gray, cv2.CV_64F).var()


def find_still_windows(
    motion_scores: list[float],
    threshold: float = 50.0,
    min_frames: int = 10,
) -> list[tuple[int, int]]:
    """Find contiguous runs where motion is below threshold.

    Returns list of (start_idx, end_idx) inclusive tuples.
    """
    windows: list[tuple[int, int]] = []
    start: Optional[int] = None

    for i, score in enumerate(motion_scores):
        if score < threshold:
            if start is None:
                start = i
        else:
            if start is not None and (i - start) >= min_frames:
                windows.append((start, i - 1))
            start = None

    # Handle trailing still window
    if start is not None and (len(motion_scores) - start) >= min_frames:
        windows.append((start, len(motion_scores) - 1))

    return windows


def pick_best_frame(frames: list[np.ndarray]) -> int:
    """Return index of the sharpest frame."""
    scores = []
    for f in frames:
        gray = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) if len(f.shape) == 3 else f
        scores.append(sharpness_score(gray))
    return int(np.argmax(scores))


# --- High-level extraction ---

DOWNSCALE_WIDTH = 320
MIN_STILL_SECONDS = 0.3
MOTION_THRESHOLD = 50.0
MIN_SHARPNESS = 20.0


def extract_frames(video_path: Path, on_progress: Optional[callable] = None) -> list[Path]:
    """Extract the best frame from each still window in a video.

    Args:
        video_path: Path to the video file (.mov, .mp4, .m4v).
        on_progress: Optional callback(stage, current, total) for progress updates.

    Returns:
        List of Paths to extracted JPEG images, in video order.
        These are normalized (rotated, resized to 1600px max, JPEG).
    """
    cap = cv2.VideoCapture(str(video_path))
    if not cap.isOpened():
        raise ValueError(f"Cannot open video: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    min_still_frames = max(int(fps * MIN_STILL_SECONDS), 5)

    if on_progress:
        on_progress("reading", 0, total_frames)

    # Pass 1: compute motion scores at low resolution
    motion_scores: list[float] = []
    prev_gray: Optional[np.ndarray] = None
    frame_idx = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        h, w = frame.shape[:2]
        scale = DOWNSCALE_WIDTH / w
        small = cv2.resize(frame, (DOWNSCALE_WIDTH, int(h * scale)))
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

        if prev_gray is not None:
            diff = cv2.absdiff(gray, prev_gray)
            motion_scores.append(float(diff.sum()) / diff.size)
        else:
            motion_scores.append(0.0)

        prev_gray = gray
        frame_idx += 1

        if on_progress and frame_idx % 100 == 0:
            on_progress("reading", frame_idx, total_frames)

    cap.release()

    if not motion_scores:
        raise ValueError("Video contains no frames")

    # Find still windows
    windows = find_still_windows(motion_scores, MOTION_THRESHOLD, min_still_frames)

    if not windows:
        raise ValueError(
            "No cards detected — no still moments found. "
            "Try holding each card still for about a second."
        )

    logger.info("Found %d still windows in %d frames (%.1f fps)", len(windows), total_frames, fps)

    if on_progress:
        on_progress("extracting", 0, len(windows))

    # Pass 2: re-read video, extract best frame from each window
    cap = cv2.VideoCapture(str(video_path))
    output_dir = video_path.parent / f"{video_path.stem}_frames"
    output_dir.mkdir(exist_ok=True)

    extracted: list[Path] = []
    window_idx = 0
    frame_idx = 0
    window_frames: list[tuple[int, np.ndarray]] = []
    current_window = windows[window_idx] if windows else None

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        if current_window and current_window[0] <= frame_idx <= current_window[1]:
            window_frames.append((frame_idx, frame))

        if current_window and frame_idx == current_window[1]:
            # End of window — pick best frame
            frames_only = [f for _, f in window_frames]
            best_idx = pick_best_frame(frames_only)
            best_frame = frames_only[best_idx]

            # Check sharpness minimum
            gray = cv2.cvtColor(best_frame, cv2.COLOR_BGR2GRAY)
            sharp = sharpness_score(gray)
            if sharp < MIN_SHARPNESS:
                logger.warning(
                    "Window %d: best frame sharpness %.1f below minimum %.1f, skipping",
                    window_idx, sharp, MIN_SHARPNESS,
                )
            else:
                out_path = output_dir / f"frame_{window_idx:04d}.jpg"
                cv2.imwrite(str(out_path), best_frame, [cv2.IMWRITE_JPEG_QUALITY, 92])
                normalized = normalize(out_path)
                extracted.append(normalized)

            window_frames = []
            window_idx += 1
            current_window = windows[window_idx] if window_idx < len(windows) else None

            if on_progress:
                on_progress("extracting", window_idx, len(windows))

        frame_idx += 1

    cap.release()

    logger.info("Extracted %d frames from %d still windows", len(extracted), len(windows))
    return extracted
