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

from app.config import settings
from app.utils.images import normalize

logger = logging.getLogger(__name__)

# --- Low-level helpers (tested directly) ---

def sharpness_score(gray: np.ndarray) -> float:
    """Laplacian variance — higher means sharper."""
    return cv2.Laplacian(gray, cv2.CV_64F).var()


def adaptive_threshold(scores: np.ndarray) -> float:
    """Derive a motion threshold from the score distribution itself.

    Card-scanning videos are bimodal: low scores while holding a card
    still, high scores during flips/transitions. The threshold sits
    between the two clusters, computed as median + 1.5 * IQR (classic
    outlier fence). Falls back to the config value as a floor.
    """
    median = float(np.median(scores))
    q25, q75 = float(np.percentile(scores, 25)), float(np.percentile(scores, 75))
    iqr = q75 - q25
    derived = median + 1.5 * iqr
    # Don't go below a sensible floor — avoids nonsense on very still videos
    floor = max(settings.video_motion_threshold, 2.0)
    return max(derived, floor)


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


def merge_nearby_windows(
    windows: list[tuple[int, int]],
    min_gap_frames: int = 15,
) -> list[tuple[int, int]]:
    """Merge windows separated by fewer than min_gap_frames.

    Fixes fragmentation where one hold gets split by a momentary
    tremor spike.
    """
    if not windows:
        return windows
    merged = [windows[0]]
    for start, end in windows[1:]:
        prev_start, prev_end = merged[-1]
        if start - prev_end <= min_gap_frames:
            merged[-1] = (prev_start, end)
        else:
            merged.append((start, end))
    return merged


def pick_best_frame(frames: list[np.ndarray]) -> int:
    """Return index of the sharpest frame."""
    scores = []
    for f in frames:
        gray = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY) if len(f.shape) == 3 else f
        scores.append(sharpness_score(gray))
    return int(np.argmax(scores))


# --- High-level extraction ---

DOWNSCALE_WIDTH = 320


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
    motion_threshold = settings.video_motion_threshold
    min_still_seconds = settings.video_min_still_seconds
    min_sharpness = settings.video_min_sharpness
    min_still_frames = max(int(fps * min_still_seconds), 3)

    if on_progress:
        on_progress("reading", 0, total_frames)

    # Pass 1: compute motion scores using HSV content differencing
    # HSV is more robust than grayscale — lighting changes mostly affect
    # value (brightness), while card transitions change hue and saturation.
    motion_scores: list[float] = []
    prev_hsv: Optional[np.ndarray] = None
    frame_idx = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        h, w = frame.shape[:2]
        scale = DOWNSCALE_WIDTH / w
        small = cv2.resize(frame, (DOWNSCALE_WIDTH, int(h * scale)))
        hsv = cv2.cvtColor(small, cv2.COLOR_BGR2HSV).astype(np.float32)

        if prev_hsv is not None:
            diff = np.abs(hsv - prev_hsv)
            # Weight: hue and saturation matter most, brightness least
            weighted = diff[:, :, 0] * 1.0 + diff[:, :, 1] * 1.0 + diff[:, :, 2] * 0.5
            motion_scores.append(float(weighted.sum()) / weighted.size)
        else:
            motion_scores.append(0.0)

        prev_hsv = hsv
        frame_idx += 1

        if on_progress and frame_idx % 100 == 0:
            on_progress("reading", frame_idx, total_frames)

    cap.release()

    if not motion_scores:
        raise ValueError("Video contains no frames")

    # Adaptive threshold: derive from this video's own score distribution
    scores_arr = np.array(motion_scores)
    threshold = adaptive_threshold(scores_arr)

    logger.info(
        "Motion scores (HSV) — min=%.1f median=%.1f p75=%.1f p90=%.1f max=%.1f | adaptive_threshold=%.1f",
        float(np.min(scores_arr)), float(np.median(scores_arr)),
        float(np.percentile(scores_arr, 75)), float(np.percentile(scores_arr, 90)),
        float(np.max(scores_arr)), threshold,
    )

    # Find still windows, then merge nearby ones (fixes fragmentation from tremor)
    merge_gap = max(int(fps * 0.3), 5)  # merge windows within 0.3s of each other
    windows = find_still_windows(motion_scores, threshold, min_still_frames)
    pre_merge_count = len(windows)
    windows = merge_nearby_windows(windows, merge_gap)

    if not windows:
        raise ValueError(
            "No cards detected — no still moments found. "
            "Try holding each card still for about a second. "
            f"(threshold={threshold:.1f}, min_frames={min_still_frames}, "
            f"median_motion={float(np.median(scores_arr)):.1f})"
        )

    logger.info("Found %d still windows (%d before merge) in %d frames (%.1f fps, threshold=%.1f, min_still=%d)",
                len(windows), pre_merge_count, total_frames, fps, threshold, min_still_frames)

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
            if sharp < min_sharpness:
                logger.warning(
                    "Window %d: best frame sharpness %.1f below minimum %.1f, skipping",
                    window_idx, sharp, min_sharpness,
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
