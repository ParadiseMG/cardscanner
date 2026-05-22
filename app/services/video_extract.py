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

# ---------------------------------------------------------------------------
# Card-aware orientation and quality helpers
# ---------------------------------------------------------------------------

# Trading card aspect ratio range (width / height in portrait). Standard
# cards are 2.5" × 3.5" = 0.714. Allow generous tolerance for sleeves,
# penny holders, and slight camera angle.
_CARD_ASPECT_LO = 0.55
_CARD_ASPECT_HI = 0.85
_MIN_CARD_AREA_PCT = 0.03  # card must be ≥3% of frame area


def _find_card_rect(img: np.ndarray) -> Optional[tuple]:
    """Find the largest card-shaped rectangle in the image.

    Returns the cv2.minAreaRect tuple ((cx,cy), (w,h), angle) or None.
    """
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (7, 7), 0)
    edges = cv2.Canny(blurred, 30, 100)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    dilated = cv2.dilate(edges, kernel, iterations=3)

    contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None

    img_area = h * w
    for cnt in sorted(contours, key=cv2.contourArea, reverse=True)[:5]:
        area = cv2.contourArea(cnt)
        if area < img_area * _MIN_CARD_AREA_PCT:
            continue
        rect = cv2.minAreaRect(cnt)
        _, (rw, rh), _ = rect
        if min(rw, rh) < 50:
            continue
        aspect = min(rw, rh) / max(rw, rh)
        if _CARD_ASPECT_LO < aspect < _CARD_ASPECT_HI:
            return rect
    return None


def detect_card_rotation(img: np.ndarray) -> Optional[int]:
    """Detect whether a frame's card is sideways.

    Returns a cv2 rotation constant (``ROTATE_90_COUNTERCLOCKWISE`` etc.)
    or *None* when the card is already upright or undetectable.
    """
    rect = _find_card_rect(img)
    if rect is None:
        return None

    (_, _), (rw, rh), angle = rect

    # Determine the angle of the card's long axis from horizontal
    if rw > rh:
        long_axis_deg = angle % 180
    else:
        long_axis_deg = (angle + 90) % 180

    is_horizontal = long_axis_deg < 35 or long_axis_deg > 145
    if not is_horizontal:
        return None  # card long-axis is vertical → already upright

    # Card is sideways. Default to CCW (the common case for portrait-mode
    # phone videos where the card is on a flat surface).
    return cv2.ROTATE_90_COUNTERCLOCKWISE


def auto_orient_frame(frame: np.ndarray,
                      forced_rotation: Optional[int] = None) -> np.ndarray:
    """Rotate *frame* so the card is upright.

    If *forced_rotation* is given (from a prior batch-level decision), it is
    applied unconditionally. Otherwise ``detect_card_rotation`` is called.
    """
    rot = forced_rotation if forced_rotation is not None else detect_card_rotation(frame)
    if rot is None:
        return frame
    return cv2.rotate(frame, rot)


def frame_has_card(img: np.ndarray, min_area_pct: float = 0.05) -> bool:
    """Return True if the frame contains a card-shaped rectangle of
    sufficient size.  Used to reject near-blank / transition frames."""
    h, w = img.shape[:2]
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    blurred = cv2.GaussianBlur(gray, (7, 7), 0)
    edges = cv2.Canny(blurred, 30, 100)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    dilated = cv2.dilate(edges, kernel, iterations=3)

    contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return False

    img_area = h * w
    largest = max(contours, key=cv2.contourArea)
    return cv2.contourArea(largest) >= img_area * min_area_pct


def frames_nearly_identical(a: np.ndarray, b: np.ndarray,
                            threshold: float = 0.97) -> bool:
    """Return True if two frames are nearly pixel-identical.

    Uses normalised absolute difference — catches the case where
    the same still window gets split into two due to motion threshold
    fragmentation and produces a literal duplicate frame.  Does NOT
    attempt semantic "same card" detection (too unreliable with shared
    backgrounds).
    """
    if a.shape != b.shape:
        # Resize smaller to match (handles very minor crop differences)
        target = (min(a.shape[1], b.shape[1]), min(a.shape[0], b.shape[0]))
        a = cv2.resize(a, target)
        b = cv2.resize(b, target)
    diff = cv2.absdiff(a, b)
    score = 1.0 - (diff.mean() / 255.0)
    return score >= threshold

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
    prev_frame: Optional[np.ndarray] = None   # for consecutive dedup
    batch_rotation: Optional[int] = None      # determined once, applied to all
    batch_rotation_decided = False
    window_idx = 0
    frame_idx = 0
    frame_counter = 0   # sequential counter for output filenames (skip gaps)
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
                # --- Card content check: skip near-blank frames ---
                if not frame_has_card(best_frame):
                    logger.warning("Window %d: no card detected in frame, skipping", window_idx)
                else:
                    # --- Auto-orient: fix sideways cards ---
                    if not batch_rotation_decided:
                        batch_rotation = detect_card_rotation(best_frame)
                        batch_rotation_decided = True
                        if batch_rotation is not None:
                            label = {cv2.ROTATE_90_CLOCKWISE: "90° CW",
                                     cv2.ROTATE_90_COUNTERCLOCKWISE: "90° CCW",
                                     cv2.ROTATE_180: "180°"}.get(batch_rotation, str(batch_rotation))
                            logger.info("Auto-orient: rotating all frames %s", label)

                    oriented = auto_orient_frame(best_frame, batch_rotation)

                    # --- Consecutive-frame duplicate detection ---
                    if prev_frame is not None and frames_nearly_identical(oriented, prev_frame):
                        logger.info("Window %d: near-identical to previous frame, skipping", window_idx)
                    else:
                        out_path = output_dir / f"frame_{frame_counter:04d}.jpg"
                        cv2.imwrite(str(out_path), oriented, [cv2.IMWRITE_JPEG_QUALITY, 92])
                        normalized = normalize(out_path)
                        extracted.append(normalized)
                        prev_frame = oriented
                        frame_counter += 1

            window_frames = []
            window_idx += 1
            current_window = windows[window_idx] if window_idx < len(windows) else None

            if on_progress:
                on_progress("extracting", window_idx, len(windows))

        frame_idx += 1

    cap.release()

    skipped = len(windows) - len(extracted)
    logger.info("Extracted %d frames from %d still windows (%d skipped: blank/duplicate/blur)",
                len(extracted), len(windows), skipped)
    return extracted
