"""Bulk-upload / job status / per-job result endpoints."""
from __future__ import annotations

import asyncio
import shutil
import uuid
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, File, Header, HTTPException, UploadFile
from sqlmodel import Session, select

from app import models, pipeline
from app.config import UPLOAD_DIR
from app.db import get_engine

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
            "started_at": job.started_at.isoformat() if job.started_at else None,
            "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        }


@router.get("/scans/jobs")
def list_jobs() -> list[dict]:
    with Session(get_engine()) as s:
        jobs = s.exec(select(models.ScanJob).order_by(models.ScanJob.id.desc()).limit(20)).all()
        return [
            {"id": j.id, "status": j.status, "total": j.total, "processed": j.processed,
             "failed": j.failed, "label": j.label,
             "started_at": j.started_at.isoformat() if j.started_at else None,
             "finished_at": j.finished_at.isoformat() if j.finished_at else None}
            for j in jobs
        ]
