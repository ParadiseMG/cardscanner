# Video Scanning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add video upload support so users can film a stack of cards (show front, flip, show back, next) and have the system extract clear frames, pair them, and feed them into the existing scan pipeline.

**Architecture:** New `video_extract.py` service handles OpenCV-based frame extraction. A new `/api/scans/upload-video` endpoint accepts video files, kicks off extraction, then hands paired images to the existing `pipeline.run_job()`. Frontend changes are minimal — accept video files in the existing drop zone and show extraction progress.

**Tech Stack:** OpenCV (opencv-python-headless), existing FastAPI + SQLModel + Ollama/Claude vision stack.

**Spec:** `docs/2026-05-21-video-scanning-design.md`

---

### Task 1: Add opencv-python-headless dependency

**Files:**
- Modify: `requirements.txt`

- [ ] **Step 1: Add dependency**

Add to `requirements.txt`:
```
opencv-python-headless==4.10.0.84
```

- [ ] **Step 2: Install and verify**

Run: `pip install opencv-python-headless==4.10.0.84`
Expected: Installs successfully.

Run: `python -c "import cv2; print(cv2.__version__)"`
Expected: `4.10.0`

- [ ] **Step 3: Commit**

```bash
git add requirements.txt
git commit -m "feat: add opencv-python-headless for video frame extraction"
```

---

### Task 2: Add video_retake_photo_threshold to Settings

**Files:**
- Modify: `app/config.py:48-55` (after the ollama settings block)

- [ ] **Step 1: Add setting**

Add after the `ollama_vision_model` line in `app/config.py`:

```python
    # Video scanning
    video_retake_photo_threshold: float = 10.0  # suggest retaking photos above this value
    video_max_upload_mb: int = 500
```

- [ ] **Step 2: Verify settings load**

Run: `python -c "from app.config import settings; print(settings.video_retake_photo_threshold, settings.video_max_upload_mb)"`
Expected: `10.0 500`

- [ ] **Step 3: Commit**

```bash
git add app/config.py
git commit -m "feat: add video scanning settings (retake threshold, max upload size)"
```

---

### Task 3: Add ScanJob model fields for video source tracking

**Files:**
- Modify: `app/models.py:144-160` (ScanJob class)
- Modify: `app/migrations.py` (add migration 12)

- [ ] **Step 1: Add fields to ScanJob model**

Add these fields to the `ScanJob` class in `app/models.py` after the `storage_position` field:

```python
    source: str = "photo"  # "photo" or "video"
    extraction_total: Optional[int] = None
    extraction_done: Optional[int] = None
```

- [ ] **Step 2: Add migration 12**

Add to `app/migrations.py` before the `MIGRATIONS` list:

```python
def m12_video_scanning(engine: Engine) -> None:
    with engine.connect() as conn:
        for col, typedef in [
            ("source", "TEXT DEFAULT 'photo'"),
            ("extraction_total", "INTEGER"),
            ("extraction_done", "INTEGER"),
        ]:
            try:
                conn.execute(text(f"ALTER TABLE scanjob ADD COLUMN {col} {typedef}"))
            except Exception:
                pass
        conn.commit()
```

Then add to the `MIGRATIONS` list:

```python
    (12, "video scanning fields on scanjob", m12_video_scanning),
```

- [ ] **Step 3: Verify migration runs**

Run: `python -c "from app.migrations import run; from app.config import settings; from sqlmodel import create_engine; e = create_engine(f'sqlite:///{settings.db_path}'); run(e)"`
Expected: No errors. Migration 12 applied.

- [ ] **Step 4: Commit**

```bash
git add app/models.py app/migrations.py
git commit -m "feat: add source/extraction fields to ScanJob for video scanning"
```

---

### Task 4: Build video frame extraction service

**Files:**
- Create: `app/services/video_extract.py`
- Create: `tests/test_video_extract.py`

- [ ] **Step 1: Write tests for frame extraction helpers**

Create `tests/test_video_extract.py`:

```python
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
    # Simulate: motion, still (10 frames), motion, still (10 frames), motion
    scores = (
        [500.0] * 5    # motion
        + [10.0] * 15  # still window 1
        + [500.0] * 5  # motion
        + [10.0] * 15  # still window 2
        + [500.0] * 5  # motion
    )
    windows = find_still_windows(scores, threshold=50.0, min_frames=10)
    assert len(windows) == 2
    # Each window is a (start_idx, end_idx) tuple
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_video_extract.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.video_extract'`

- [ ] **Step 3: Implement video_extract.py**

Create `app/services/video_extract.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_video_extract.py -v`
Expected: All 5 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/video_extract.py tests/test_video_extract.py
git commit -m "feat: add video frame extraction service with motion detection"
```

---

### Task 5: Add front/back verification

**Files:**
- Create: `app/services/video_pair.py`
- Create: `tests/test_video_pair.py`

- [ ] **Step 1: Write tests**

Create `tests/test_video_pair.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_video_pair.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement video_pair.py**

Create `app/services/video_pair.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_video_pair.py -v`
Expected: All 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/video_pair.py tests/test_video_pair.py
git commit -m "feat: add front/back pairing with vision verification for video frames"
```

---

### Task 6: Add video upload endpoint and wire into pipeline

**Files:**
- Modify: `app/routers/scan.py`
- Modify: `app/pipeline.py`
- Modify: `app/main.py` (only if scan router not already registered — it is, so no change needed)

- [ ] **Step 1: Add video upload endpoint to scan.py**

Add these imports to the top of `app/routers/scan.py`:

```python
from app.services.video_extract import extract_frames
from app.services.video_pair import classify_sides, pair_frames
```

Add this endpoint after the existing `upload` function:

```python
VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v"}
VIDEO_TMP_DIR = Path("/data/video-tmp")


@router.post("/scans/upload-video")
async def upload_video(
    file: UploadFile = File(...),
    label: Optional[str] = None,
    x_anthropic_key: Optional[str] = Header(default=None),
) -> dict:
    """Upload a video of cards for scanning.

    Extracts clear frames, pairs front/back, and feeds into the scan pipeline.
    """
    suffix = Path(file.filename or "video.mp4").suffix.lower()
    if suffix not in VIDEO_EXTENSIONS:
        raise HTTPException(400, f"Unsupported format {suffix}. Use .mov, .mp4, or .m4v")

    max_bytes = settings.video_max_upload_mb * 1024 * 1024
    VIDEO_TMP_DIR.mkdir(parents=True, exist_ok=True)
    video_path = VIDEO_TMP_DIR / f"{uuid4().hex}{suffix}"

    # Stream to disk to avoid loading entire video into memory
    size = 0
    with open(video_path, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > max_bytes:
                video_path.unlink(missing_ok=True)
                raise HTTPException(413, f"Video exceeds {settings.video_max_upload_mb}MB limit")
            f.write(chunk)

    # Create job
    job = ScanJob(label=label or "Video scan", source="video", status="queued")
    with Session(engine) as s:
        s.add(job)
        s.commit()
        s.refresh(job)
        job_id = job.id

    asyncio.create_task(_process_video(job_id, video_path, x_anthropic_key))
    return {"job_id": job_id, "source": "video"}


async def _process_video(
    job_id: int, video_path: Path, api_key_override: Optional[str]
) -> None:
    """Background task: extract frames from video, pair, then run scan pipeline."""
    from app.pipeline import run_job

    try:
        # Update job status
        with Session(engine) as s:
            job = s.get(ScanJob, job_id)
            job.status = "processing"
            s.commit()

        # Extract frames
        def on_progress(stage, current, total):
            with Session(engine) as s:
                job = s.get(ScanJob, job_id)
                if stage == "extracting":
                    job.extraction_total = total
                    job.extraction_done = current
                s.commit()

        frames = extract_frames(video_path, on_progress=on_progress)

        if not frames:
            with Session(engine) as s:
                job = s.get(ScanJob, job_id)
                job.status = "error"
                job.finished_at = datetime.utcnow()
                s.commit()
            return

        # Classify front/back
        sides = await classify_sides(frames)
        pairs = pair_frames(frames, sides)

        # Build image list for pipeline (front, back, front, back, ...)
        image_paths: list[Path] = []
        for pair in pairs:
            image_paths.append(pair.front)
            if pair.back:
                image_paths.append(pair.back)

        # Update job total (number of cards, not frames)
        with Session(engine) as s:
            job = s.get(ScanJob, job_id)
            job.total = len(pairs)
            job.extraction_total = len(frames)
            job.extraction_done = len(frames)
            s.commit()

        # Hand off to existing pipeline
        await run_job(job_id, image_paths, api_key_override)

    except Exception as e:
        logger.exception("Video processing failed for job %d", job_id)
        with Session(engine) as s:
            job = s.get(ScanJob, job_id)
            job.status = "error"
            job.finished_at = datetime.utcnow()
            s.commit()
    finally:
        # Clean up temp video
        video_path.unlink(missing_ok=True)
```

Also add these imports if not already present at the top of scan.py:

```python
import asyncio
import logging
from uuid import uuid4
from datetime import datetime
from pathlib import Path
from fastapi import HTTPException
from sqlmodel import Session
from app.config import settings
from app.models import ScanJob
```

- [ ] **Step 2: Update pipeline.py to handle video pairs**

The existing `run_job` uses `auto_pair()` to pair images by EXIF time. Video frames won't have meaningful EXIF timestamps, but they're already paired — they arrive as [front1, back1, front2, back2, ...].

Check if `auto_pair` gracefully handles images without EXIF timestamps (falls back to sequential pairing). If not, add a check in `run_job`:

In `app/pipeline.py`, modify the `run_job` function near line 213 where it calls `auto_pair(image_paths)`. Wrap it:

```python
    # Auto-pair by EXIF timestamp. For video sources, frames arrive
    # pre-paired (front, back, front, back, ...) and lack EXIF times,
    # so auto_pair falls back to sequential ordering.
    paired = auto_pair(image_paths)
```

If `auto_pair` doesn't handle missing EXIF gracefully, add a fallback at the top of the `auto_pair` function in `app/services/auto_pair.py` that pairs sequentially when no timestamps are available.

- [ ] **Step 3: Add listing photo threshold check**

In `app/pipeline.py`, in the `_process_one` function, after comp lookup sets `est_value_raw`, add:

```python
        # Flag cards above threshold for photo retake (useful for video-sourced scans)
        if card.est_value_raw and card.est_value_raw >= settings.video_retake_photo_threshold:
            card.needs_photo_verification = True
```

- [ ] **Step 4: Test manually with a short test video**

Record a 10-second video showing 2 cards (front, flip, back each). Upload via:

```bash
curl -X POST http://localhost:8765/api/scans/upload-video \
  -F "file=@test_video.mov" \
  -F "label=test video"
```

Expected: Returns `{"job_id": N, "source": "video"}`. Poll `/api/scans/jobs/N` until done.

- [ ] **Step 5: Commit**

```bash
git add app/routers/scan.py app/pipeline.py
git commit -m "feat: add video upload endpoint with extraction and pipeline integration"
```

---

### Task 7: Update frontend to accept video uploads

**Files:**
- Modify: `app/static/index.html:143-159` (drop zone)
- Modify: `app/static/app.js:955-986` (upload and poll handlers)

- [ ] **Step 1: Update file input accept filter**

In `app/static/index.html`, change the file input (around line 153):

```html
<input type="file" multiple accept="image/*,video/mp4,video/quicktime,video/x-m4v,.mov,.mp4,.m4v" class="hidden" @change="onFiles($event.target.files)">
```

- [ ] **Step 2: Update upload handler to detect video files**

In `app/static/app.js`, replace the `uploadFiles` method:

```javascript
async uploadFiles(files) {
  if (!files || !files.length) return;

  // Check if any file is a video
  const videoFile = Array.from(files).find(f =>
    /\.(mov|mp4|m4v)$/i.test(f.name) || f.type.startsWith('video/')
  );

  const headers = {};
  const key = localStorage.getItem('anthropic_key');
  if (key) headers['X-Anthropic-Key'] = key;

  if (videoFile) {
    // Video upload — single file to video endpoint
    const fd = new FormData();
    fd.append('file', videoFile);
    fd.append('label', `Video scan`);
    const res = await fetch('/api/scans/upload-video', { method: 'POST', body: fd, headers }).then(r => r.json());
    if (res.error || res.detail) { alert(res.detail || res.error); return; }
    this.job = { id: res.job_id, total: 0, processed: 0, status: 'queued', source: 'video' };
  } else {
    // Photo upload — existing behavior
    const fd = new FormData();
    for (const f of files) fd.append('files', f);
    fd.append('label', `Batch of ${files.length}`);
    const res = await fetch('/api/scans/upload', { method: 'POST', body: fd, headers }).then(r => r.json());
    this.job = { id: res.job_id, total: res.queued, processed: 0, status: 'queued', source: 'photo' };
  }

  if (this.jobPoll) clearInterval(this.jobPoll);
  this.jobPoll = setInterval(() => this.pollJob(), 1500);
},
```

- [ ] **Step 3: Update job polling to show extraction progress**

In the `pollJob` method, update the status display to handle the extraction phase:

```javascript
async pollJob() {
  if (!this.job) return;
  const j = await fetch('/api/scans/jobs/' + this.job.id).then(r => r.json());
  this.job = { ...this.job, ...j };
  if (j.status === 'done') {
    clearInterval(this.jobPoll);
    await this.refreshAll();
    setTimeout(() => { this.job = null; }, 3000);
  }
},
```

- [ ] **Step 4: Update job status display in index.html**

Find the job progress display in `index.html` and update it to show extraction progress for video jobs. Where the current template shows processing progress, add:

```html
<template x-if="job">
  <div class="rounded-xl bg-slate-900/80 border border-slate-700 p-4 mb-4">
    <template x-if="job.source === 'video' && job.extraction_total && job.extraction_done < job.extraction_total">
      <p class="text-sm text-slate-300">
        🎬 Extracting frames... <span x-text="job.extraction_done"></span>/<span x-text="job.extraction_total"></span>
      </p>
    </template>
    <template x-if="job.source === 'video' && job.extraction_total && job.extraction_done >= job.extraction_total && job.status === 'processing'">
      <p class="text-sm text-slate-300">
        🃏 Processing card <span x-text="job.processed"></span>/<span x-text="job.total"></span>...
      </p>
    </template>
    <template x-if="job.source !== 'video'">
      <!-- existing photo job progress display -->
    </template>
    <template x-if="job.status === 'done'">
      <p class="text-sm text-green-400">✅ Done! <span x-text="job.processed"></span> cards scanned.</p>
    </template>
    <template x-if="job.status === 'error'">
      <p class="text-sm text-red-400">❌ Error processing job.</p>
    </template>
  </div>
</template>
```

- [ ] **Step 5: Update job_status endpoint to return new fields**

In `app/routers/scan.py`, update the `job_status` endpoint to include the new fields:

```python
@router.get("/scans/jobs/{job_id}")
def job_status(job_id: int) -> dict:
    with Session(engine) as s:
        job = s.get(ScanJob, job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        return {
            "id": job.id,
            "status": job.status,
            "total": job.total,
            "processed": job.processed,
            "failed": job.failed,
            "source": job.source,
            "extraction_total": job.extraction_total,
            "extraction_done": job.extraction_done,
            "started_at": str(job.started_at) if job.started_at else None,
            "finished_at": str(job.finished_at) if job.finished_at else None,
        }
```

- [ ] **Step 6: Test end-to-end in browser**

1. Start dev server: `uvicorn app.main:app --host 0.0.0.0 --port 8765 --reload`
2. Open http://localhost:8765
3. Drag a .mov file into the upload zone
4. Verify: extraction progress shows, then card processing, then done
5. Check inventory for the scanned cards

- [ ] **Step 7: Commit**

```bash
git add app/static/index.html app/static/app.js app/routers/scan.py
git commit -m "feat: frontend video upload with extraction progress display"
```

---

### Task 8: Add needs_photo_verification badge to inventory UI

**Files:**
- Modify: `app/static/index.html` (card list/grid item template)
- Modify: `app/static/app.js` (if any filtering logic needed)

- [ ] **Step 1: Find the card display template in index.html**

Locate where individual cards are rendered in the inventory list. Add a badge when `needs_photo_verification` is true:

```html
<span x-show="card.needs_photo_verification"
      class="inline-block px-2 py-0.5 text-xs rounded-full bg-amber-900/50 text-amber-300 border border-amber-700">
  📸 Retake photos for listing
</span>
```

Place this near the card's value display or status badges.

- [ ] **Step 2: Test in browser**

Verify the badge appears on cards with `needs_photo_verification = True` and doesn't appear on others.

- [ ] **Step 3: Commit**

```bash
git add app/static/index.html app/static/app.js
git commit -m "feat: add retake-photos badge for high-value video-scanned cards"
```

---

### Task 9: Update Dockerfile for video tmp directory

**Files:**
- Modify: `Dockerfile`
- Modify: `docker-compose.yml` (if volume mapping needed)

- [ ] **Step 1: Add video-tmp directory to Dockerfile**

In `Dockerfile`, after the `RUN mkdir -p /data` line:

```dockerfile
RUN mkdir -p /data/video-tmp
```

- [ ] **Step 2: Add opencv system deps if needed**

`opencv-python-headless` is pure Python wheels on most platforms, but verify the Docker build works. In the Dockerfile, the existing system deps for Playwright should cover it. No additional packages needed.

- [ ] **Step 3: Build and test Docker image**

```bash
docker build -t cardscanner:video-test .
docker run --rm cardscanner:video-test python -c "import cv2; print(cv2.__version__)"
```

Expected: Prints OpenCV version without errors.

- [ ] **Step 4: Commit**

```bash
git add Dockerfile
git commit -m "feat: add video-tmp directory to Docker image"
```

---

### Task 10: Push and redeploy

**Files:** None (git + Portainer operations)

- [ ] **Step 1: Push to GitHub**

```bash
git push origin master
```

- [ ] **Step 2: Redeploy via Portainer**

Use Portainer API to redeploy stack 4 (cardscanner) to pick up the new code.

- [ ] **Step 3: Verify health endpoint**

```bash
curl http://100.71.106.53:8765/api/health
```

Expected: `{"ok": true, ...}` with Ollama backend connected.

- [ ] **Step 4: Test video upload on server**

Upload a test video through the web UI at `http://100.71.106.53:8765` and verify end-to-end.
