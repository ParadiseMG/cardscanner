# Video Scanning Design

**Date:** 2026-05-21
**Status:** Approved
**Goal:** Scan cards by video instead of individual photos. Film a stack of cards (show front, flip, show back, next card) in one continuous video. The system extracts clear frames, pairs front/back, and feeds them into the existing scan pipeline.

## Motivation

Taking individual photos of each card's front and back is the biggest bottleneck in the scanning workflow. Video lets you sweep through a stack in a fraction of the time — just hold, flip, set down, next.

## Physical Workflow

1. Start recording on iPhone (1080p recommended, 4K works but no benefit)
2. Hold card facing camera, pause briefly (~1 second)
3. Flip card, pause briefly
4. Set card down, pick up next card
5. Repeat until stack is done
6. Stop recording, upload video through web UI

## Architecture

### New Components

- **`app/services/video_extract.py`** — frame extraction engine (OpenCV)
- **`POST /api/scans/upload-video`** — video upload endpoint
- Frontend: accept video files in existing drag/drop zone

### Unchanged Components

Everything downstream of frame extraction is unchanged: `pipeline.run_job()`, vision identification, comp lookup, Google Sheets sync, inventory UI, eBay integration.

## Frame Extraction Pipeline

### Step 1: Motion Analysis

1. Read video with OpenCV
2. Convert each frame to grayscale, downscale to 320px wide (for fast diffing)
3. Compute absolute difference between consecutive frames, sum pixel deltas to get a "motion score"
4. Identify "still windows" where motion score stays below threshold for 10+ consecutive frames (~0.3s at 30fps)

### Step 2: Best Frame Selection

From each still window, go back to full-resolution frames and compute Laplacian variance (sharpness score). Pick the sharpest frame.

### Step 3: Image Normalization

Run winning frames through the existing image pipeline:
- EXIF rotation
- 1600px max edge resize
- JPEG conversion at quality 88

### Step 4: Sequential Pairing

Group frames as [front1, back1, front2, back2, ...] based on sequence order.

### Step 5: Front/Back Verification

Send each frame to vision (Ollama preferred — cheap, local) with a lightweight prompt: "Is this the front or back of a trading card? Reply 'front' or 'back'."

- If sequence matches expectation (alternating front/back): proceed
- If two consecutive frames are the same side: flag the pair for manual review
- Odd number of frames: last card flagged as incomplete, still processed for identification

### Step 6: Handoff

Paired images feed into `pipeline.run_job()` exactly like photo uploads.

### Step 7: Cleanup

Delete temp video file after extraction. Keep extracted frames as the card's `front_image`/`back_image`.

## API Changes

### New Endpoint

```
POST /api/scans/upload-video
Content-Type: multipart/form-data
Body: video file + optional batch_label
Returns: { job_id: string }
```

- Max file size: 500MB
- Accepted formats: .mov, .mp4, .m4v
- Rejects: under 2 seconds, over 500MB

### ScanJob Model Changes

New fields on `ScanJob`:
- `source: str` — `"photo"` (default) or `"video"`
- `extraction_total: int | None` — total frames extracted from video
- `extraction_done: int | None` — frames processed so far

No changes to the Card model. By the time cards enter the pipeline, they're just images.

## Frontend Changes

- Drag/drop zone accepts `.mov`, `.mp4`, `.m4v` in addition to images
- File type detection: video files POST to `/api/scans/upload-video`
- Job status display adds extraction phase:
  - "Extracting frames..." → "Extracted 14 frames (7 cards)" → "Processing card 3/7..." → "Done"
- Cards with `needs_photo_verification = True` show badge: "Consider retaking photos for listing"

No changes to inventory view, card detail, comp display, bulk actions, or any other UI.

## Listing Photo Threshold

After comp lookup, if `est_value_raw >= $10`, set `needs_photo_verification = True`. Surfaced in the UI as a suggestion to retake photos for listing quality. Threshold is configurable via `VIDEO_RETAKE_PHOTO_THRESHOLD` env var (default: 10.0).

## Edge Cases

| Scenario | Behavior |
|----------|----------|
| No still frames detected | Job fails: "No cards detected. Try holding each card still for a moment." |
| Odd number of frames | Last card flagged incomplete, front-only identification attempted |
| Two fronts/backs in a row | Pair flagged for manual review, not silently mispaired |
| Very long video (>500MB) | Rejected at upload with clear error message |
| Very short video (<2s) | Rejected at upload |
| Blurry throughout | Still windows found but sharpness below minimum threshold — frame skipped with warning |

## Dependencies

- `opencv-python-headless` added to `requirements.txt` (~30MB, no GUI/X11 deps)
- Temp video storage: `/data/video-tmp/`, cleaned after extraction
- No new system packages needed in Dockerfile

## Not In Scope

- Client-side frame extraction (browser-based)
- Google Drive video upload (can add later)
- Automatic eBay listing from video frames
- Multiple camera angle support
- Real-time / streaming processing
