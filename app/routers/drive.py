"""Drive sync trigger + inbox status."""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel

from app import pipeline
from app.services import drive_inbox

router = APIRouter()


class DriveSyncBody(BaseModel):
    """Optional per-sync tagging — every Card from this sync inherits these."""
    storage_location_id: Optional[int] = None
    storage_position: Optional[str] = None


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
async def sync(body: Optional[DriveSyncBody] = None,
               x_anthropic_key: Optional[str] = Header(default=None)) -> dict:
    """Start a Drive sync. Optional body tags every new Card with a storage location."""
    loc_id = body.storage_location_id if body else None
    position = body.storage_position if body else None
    try:
        job_id = await pipeline.run_drive_sync(
            x_anthropic_key,
            storage_location_id=loc_id,
            storage_position=position,
        )
    except ValueError as e:
        raise HTTPException(400, str(e))
    except RuntimeError as e:
        raise HTTPException(412, str(e))
    return {"job_id": job_id}


@router.post("/drive/retry-failed")
def retry_failed() -> dict:
    """Move every file from `Failed/` back to `To Be Processed/` for a re-run."""
    try:
        folders = drive_inbox.ensure_folders()
    except RuntimeError as e:
        raise HTTPException(412, str(e))

    # List Failed folder
    from googleapiclient.errors import HttpError
    svc = drive_inbox._service()
    res = svc.files().list(
        q=f"'{folders.failed_id}' in parents and trashed = false",
        fields="files(id, name, mimeType)",
        pageSize=500,
    ).execute()
    files = res.get("files", [])

    moved = 0
    for f in files:
        if f.get("mimeType") not in drive_inbox.IMAGE_MIMES:
            continue
        try:
            drive_inbox.move_file(f["id"], folders.inbox_id)
            moved += 1
        except HttpError:
            continue
    return {"moved": moved, "total_in_failed": len(files)}
