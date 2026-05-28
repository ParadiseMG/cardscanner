"""Bulk-upload / job status / per-job result endpoints."""
from __future__ import annotations

import asyncio
import logging
import shutil
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, File, Form, Header, HTTPException, UploadFile
from sqlmodel import Session, select

from app import models, pipeline
from app.config import UPLOAD_DIR, settings
from app.db import get_engine
from app.models import ScanJob
from app.services.video_extract import extract_frames, check_batch_upside_down, rotate_all_frames
from app.services.video_pair import classify_sides, pair_frames

logger = logging.getLogger(__name__)

router = APIRouter()


@router.post("/scans/upload")
async def upload(
    files: list[UploadFile] = File(...),
    label: Optional[str] = None,
    x_anthropic_key: Optional[str] = Header(default=None),
) -> dict:
    if not files:
        raise HTTPException(400, "no files")

    saved: list[Path] = []
    for f in files:
        if not f.filename:
            continue
        suffix = Path(f.filename).suffix.lower() or ".jpg"
        unique = f"{uuid.uuid4().hex}{suffix}"
        dest = UPLOAD_DIR / unique
        with dest.open("wb") as out:
            shutil.copyfileobj(f.file, out)
        saved.append(dest)

    with Session(get_engine()) as s:
        job = models.ScanJob(label=label, total=len(saved))
        s.add(job); s.commit(); s.refresh(job)
        job_id = job.id

    asyncio.create_task(pipeline.run_job(job_id, saved, x_anthropic_key))
    return {"job_id": job_id, "queued": len(saved)}


VIDEO_EXTENSIONS = {".mov", ".mp4", ".m4v"}
VIDEO_TMP_DIR = Path("/data/video-tmp")


@router.post("/scans/upload-video")
async def upload_video(
    file: UploadFile = File(...),
    label: Optional[str] = Form(default=None),
    batch_year: Optional[int] = Form(default=None),
    batch_set_brand: Optional[str] = Form(default=None),
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
    video_path = VIDEO_TMP_DIR / f"{uuid.uuid4().hex}{suffix}"

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
    logger.info("Video upload: label=%r, batch_year=%r, batch_set_brand=%r", label, batch_year, batch_set_brand)
    batch_label = label or "Video scan"
    if batch_set_brand:
        batch_label = f"{batch_year or ''} {batch_set_brand}".strip()
    job = ScanJob(
        label=batch_label, source="video", status="queued",
        batch_year=batch_year, batch_set_brand=batch_set_brand,
    )
    with Session(get_engine()) as s:
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
    from app.pipeline import run_job_paired

    try:
        # Update job status
        with Session(get_engine()) as s:
            job = s.get(ScanJob, job_id)
            job.status = "processing"
            s.commit()

        # Extract frames
        def on_progress(stage, current, total):
            with Session(get_engine()) as s:
                job = s.get(ScanJob, job_id)
                if stage == "extracting":
                    job.extraction_total = total
                    job.extraction_done = current
                s.commit()

        frames = extract_frames(video_path, on_progress=on_progress)

        if not frames:
            with Session(get_engine()) as s:
                job = s.get(ScanJob, job_id)
                job.status = "error"
                job.finished_at = datetime.utcnow()
                s.commit()
            return

        # Check if batch is upside-down and fix
        if await check_batch_upside_down(frames):
            rotate_all_frames(frames)

        # Classify front/back
        sides = await classify_sides(frames)
        pairs = pair_frames(frames, sides)

        # Update job total (number of cards, not frames)
        with Session(get_engine()) as s:
            job = s.get(ScanJob, job_id)
            job.total = len(pairs)
            job.extraction_total = len(frames)
            job.extraction_done = len(frames)
            s.commit()

        # Hand off to existing pipeline — use run_job_paired to skip auto_pair
        await run_job_paired(job_id, pairs, api_key_override)

    except Exception:
        logger.exception("Video processing failed for job %d", job_id)
        with Session(get_engine()) as s:
            job = s.get(ScanJob, job_id)
            job.status = "error"
            job.finished_at = datetime.utcnow()
            s.commit()
    finally:
        # Clean up temp video
        video_path.unlink(missing_ok=True)


@router.get("/scans/jobs/{job_id}")
def job_status(job_id: int) -> dict:
    with Session(get_engine()) as s:
        job = s.get(models.ScanJob, job_id)
        if not job:
            raise HTTPException(404, "job not found")
        return {
            "id": job.id,
            "status": job.status,
            "total": job.total,
            "processed": job.processed,
            "failed": job.failed,
            "label": job.label,
            "source": job.source,
            "extraction_total": job.extraction_total,
            "extraction_done": job.extraction_done,
            "batch_year": job.batch_year,
            "batch_set_brand": job.batch_set_brand,
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        }


@router.get("/scans/jobs")
def list_jobs() -> list[dict]:
    with Session(get_engine()) as s:
        jobs = s.exec(select(models.ScanJob).order_by(models.ScanJob.id.desc()).limit(20)).all()
        return [
            {"id": j.id, "status": j.status, "total": j.total, "processed": j.processed,
             "failed": j.failed, "label": j.label, "source": j.source,
             "started_at": j.started_at.isoformat() if j.started_at else None,
             "finished_at": j.finished_at.isoformat() if j.finished_at else None}
            for j in jobs
        ]


@router.get("/scans/recent-failures")
def recent_failures(limit: int = 30) -> dict:
    """Most recent JobFailure rows so the dashboard can show the user WHY."""
    with Session(get_engine()) as s:
        rows = s.exec(
            select(models.JobFailure)
            .order_by(models.JobFailure.id.desc())
            .limit(limit)
        ).all()
        return {"items": [
            {"id": f.id, "scan_job_id": f.scan_job_id, "file_name": f.file_name,
             "drive_id": f.drive_id, "error": f.error,
             "error_class": f.error_class,
             "occurred_at": f.occurred_at.isoformat() if f.occurred_at else None}
            for f in rows
        ]}


@router.delete("/scans/recent-failures")
def clear_failures() -> dict:
    """Delete all JobFailure rows."""
    with Session(get_engine()) as s:
        count = s.exec(select(models.JobFailure)).all()
        for f in count:
            s.delete(f)
        s.commit()
        return {"deleted": len(count)}


@router.get("/scans/jobs/{job_id}/failures")
def job_failures(job_id: int) -> dict:
    with Session(get_engine()) as s:
        rows = s.exec(
            select(models.JobFailure)
            .where(models.JobFailure.scan_job_id == job_id)
            .order_by(models.JobFailure.id)
        ).all()
        return {"items": [
            {"id": f.id, "file_name": f.file_name, "error": f.error,
             "error_class": f.error_class,
             "occurred_at": f.occurred_at.isoformat() if f.occurred_at else None}
            for f in rows
        ]}


@router.post("/scans/jobs/{job_id}/reidentify")
async def reidentify_job(
    job_id: int,
    x_anthropic_key: Optional[str] = Header(default=None),
) -> dict:
    """Re-run vision identification on all cards from a job.

    Useful when orientation/prompt improvements mean the same images
    would produce better results now.
    """
    with Session(get_engine()) as s:
        job = s.get(models.ScanJob, job_id)
        if not job:
            raise HTTPException(404, "job not found")

        cards = s.exec(
            select(models.Card)
            .where(models.Card.scan_job_id == job_id)
        ).all()

    if not cards:
        raise HTTPException(404, "no cards found for this job")

    asyncio.create_task(_reidentify_cards(
        job_id, [c.id for c in cards], x_anthropic_key,
        batch_year=job.batch_year, batch_set_brand=job.batch_set_brand,
    ))
    return {"job_id": job_id, "cards": len(cards), "status": "reidentifying"}


async def _reidentify_cards(
    job_id: int,
    card_ids: list[int],
    api_key_override: Optional[str],
    batch_year: Optional[int] = None,
    batch_set_brand: Optional[str] = None,
) -> None:
    """Background: re-identify each card in a job."""
    import json
    from app.services import claude_vision
    from app.db import session_scope

    for card_id in card_ids:
        try:
            with Session(get_engine()) as s:
                card = s.get(models.Card, card_id)
                if not card or not card.front_image:
                    continue
                front_path = UPLOAD_DIR / card.front_image
                back_path = UPLOAD_DIR / card.back_image if card.back_image else None

            if not front_path.exists():
                logger.warning("reidentify skip card %d: front_image not found", card_id)
                continue

            ident = await claude_vision.identify_card_async(
                str(front_path),
                str(back_path) if back_path and back_path.exists() else None,
                api_key_override=api_key_override,
            )

            # Apply batch overrides
            if batch_year is not None:
                ident.year = batch_year
                ident.field_confidence["year"] = 1.0
            if batch_set_brand:
                ident.set_brand = batch_set_brand
                ident.field_confidence["set_brand"] = 1.0

            with session_scope() as s:
                card = s.get(models.Card, card_id)
                if not card:
                    continue
                for f in ("year", "set_brand", "player", "card_no", "parallel",
                          "sport", "team", "condition", "is_graded", "grade",
                          "is_autograph", "is_relic", "is_rookie",
                          "is_serial_numbered", "serial_print_run", "photo_quality"):
                    val = getattr(ident, f, None)
                    if val is not None:
                        setattr(card, f, val)
                if ident.low_confidence_fields:
                    card.low_confidence_fields = json.dumps(ident.low_confidence_fields)
                if ident.condition_signals:
                    card.condition_signals = json.dumps(ident.condition_signals)
                card.updated_at = datetime.utcnow()
                s.add(card)

            logger.info("Reidentified card %d: player=%s", card_id, ident.player)

        except Exception:
            logger.exception("Reidentify failed for card %d", card_id)
