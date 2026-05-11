"""FastAPI entrypoint for CardScanner."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings, REPO_ROOT, UPLOAD_DIR
from app.db import init_db
from app.routers import scan, inventory, stats, auth, listings, sync, drive, health as health_router
from app.routers import bulk as bulk_router
from app.routers import suggestions as suggestions_router
from app.routers import sales as sales_router
from app.routers import grading as grading_router
from app.routers import bulk_lots as bulk_lots_router
from app.utils import logger as _log


def _resume_stranded_jobs() -> None:
    """A4: On startup, handle ScanJob rows left in queued/processing state."""
    import asyncio
    import json
    from datetime import datetime, timedelta
    from sqlmodel import Session, select
    from app.db import get_engine, session_scope
    from app import models

    log = _log.get(__name__)

    with _log.step(log, "batch_resume_scan"):
        with Session(get_engine()) as s:
            stranded = s.exec(
                select(models.ScanJob).where(
                    models.ScanJob.status.in_(["queued", "processing"]),
                    models.ScanJob.finished_at.is_(None),
                )
            ).all()

        cutoff = datetime.utcnow() - timedelta(minutes=60)

        for job in stranded:
            if job.started_at and job.started_at < cutoff:
                # Too old — mark abandoned
                with session_scope() as s:
                    j = s.get(models.ScanJob, job.id)
                    if j:
                        j.status = "abandoned"
                        j.finished_at = datetime.utcnow()
                        s.add(j)
                log.info("batch_resume abandoned old job", extra={"job_id": job.id})
                continue

            # Has a manifest — try to resume the unprocessed portion
            if not job.source_manifest:
                # No manifest: no way to know what's left — mark abandoned
                with session_scope() as s:
                    j = s.get(models.ScanJob, job.id)
                    if j:
                        j.status = "abandoned"
                        j.finished_at = datetime.utcnow()
                        s.add(j)
                log.info("batch_resume abandoned no-manifest job", extra={"job_id": job.id})
                continue

            try:
                manifest = json.loads(job.source_manifest)
            except Exception:
                log.warning("batch_resume bad manifest", extra={"job_id": job.id})
                continue

            # Determine which hashes are already done
            with Session(get_engine()) as s:
                from sqlmodel import select as _sel
                done_hashes = set(
                    s.exec(_sel(models.Card.front_hash).where(
                        models.Card.scan_job_id == job.id
                    )).all()
                )

            remaining = [
                item for item in manifest
                if isinstance(item, dict) and item.get("front_hash") not in done_hashes
            ]

            if not remaining:
                with session_scope() as s:
                    j = s.get(models.ScanJob, job.id)
                    if j:
                        j.status = "done"
                        j.finished_at = datetime.utcnow()
                        s.add(j)
                log.info("batch_resume job already complete", extra={"job_id": job.id})
                continue

            log.info("batch_resume resuming job", extra={
                "job_id": job.id, "remaining": len(remaining), "total": len(manifest)
            })

            async def _resume_task(jid=job.id, items=remaining):
                from pathlib import Path
                from app import pipeline
                paths = [Path(item["path"]) for item in items if "path" in item]
                if paths:
                    await pipeline.run_job(jid, paths, api_key_override=None)

            try:
                loop = asyncio.get_running_loop()
                loop.create_task(_resume_task())
            except RuntimeError:
                pass  # No running event loop at import time; task deferred


def create_app() -> FastAPI:
    _log.configure()
    init_db()
    _resume_stranded_jobs()
    app = FastAPI(title="CardScanner", version="0.1.0")
    app.add_middleware(
        CORSMiddleware, allow_origins=["*"],
        allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
    )

    app.include_router(scan.router, prefix="/api")
    app.include_router(inventory.router, prefix="/api")
    app.include_router(stats.router, prefix="/api")
    app.include_router(auth.router, prefix="/api")
    app.include_router(listings.router, prefix="/api")
    app.include_router(sync.router, prefix="/api")
    app.include_router(drive.router, prefix="/api")
    app.include_router(health_router.router, prefix="/api")
    app.include_router(bulk_router.router, prefix="/api")
    app.include_router(suggestions_router.router, prefix="/api")
    app.include_router(sales_router.router, prefix="/api")
    app.include_router(grading_router.router, prefix="/api")
    app.include_router(bulk_lots_router.router, prefix="/api")

    # Serve uploaded card images
    app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")

    static_dir = REPO_ROOT / "app" / "static"
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    @app.get("/")
    def index():
        return FileResponse(static_dir / "index.html")

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.host, port=settings.port)
