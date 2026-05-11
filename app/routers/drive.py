"""Drive sync trigger + inbox status."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Header, HTTPException

from app import pipeline
from app.services import drive_inbox

router = APIRouter()


@router.get("/drive/status")
def status() -> dict:
    try:
        folders = drive_inbox.ensure_folders()
        files = drive_inbox.list_inbox(folders)
        pairs = drive_inbox.pair_files(files)
        return {
            "connected": True,
            "root_id": folders.root_id,
            "inbox_count": len(files),
            "pair_count": len(pairs),
        }
    except RuntimeError as e:
        return {"connected": False, "error": str(e)}
    except Exception as e:
        return {"connected": False, "error": str(e)}


@router.post("/drive/sync")
async def sync(x_anthropic_key: Optional[str] = Header(default=None)) -> dict:
    try:
        job_id = await pipeline.run_drive_sync(x_anthropic_key)
    except RuntimeError as e:
        raise HTTPException(412, str(e))
    return {"job_id": job_id}
