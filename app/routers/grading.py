"""Grading submission router.

Endpoints:
  POST /api/grading/build-submission  — returns PSA or SGC CSV as download
  POST /api/grading/queue             — marks cards as Pending Grading
  GET  /api/grading/queues            — lists active submission batches
  POST /api/grading/{submission_id}/mark-back  — records returned grades
"""
from __future__ import annotations

import io
import uuid
from datetime import datetime, date
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException, Header
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlmodel import Session, select

from app import models
from app.db import get_engine, session_scope
from app.services import grading as grading_svc
from app.utils import logger as _log

router = APIRouter()
log = _log.get(__name__)


# ---------------------------------------------------------------------------
# Request / Response schemas
# ---------------------------------------------------------------------------

class BuildSubmissionRequest(BaseModel):
    card_ids: list[int]
    service: Literal["PSA", "SGC"]
    service_level: str = "Value"


class QueueRequest(BaseModel):
    card_ids: list[int]
    service: Literal["PSA", "SGC"]
    service_level: str = "Value"


class QueueResponse(BaseModel):
    submission_id: str
    queued: int
    estimated_cost: float


class SubmissionGroup(BaseModel):
    submission_id: str
    service: Optional[str]
    service_level: Optional[str]
    card_count: int
    card_ids: list[int]
    estimated_cost: float


class QueuesResponse(BaseModel):
    submissions: list[SubmissionGroup]


class MarkBackItem(BaseModel):
    card_id: int
    grade: str   # e.g. "9" or "8.5"


class MarkBackRequest(BaseModel):
    items: list[MarkBackItem]
    service: Optional[str] = None   # "PSA" / "SGC" — used to prefix the grade string


class MarkBackResponse(BaseModel):
    updated: int


# ---------------------------------------------------------------------------
# POST /api/grading/build-submission
# ---------------------------------------------------------------------------

@router.post("/grading/build-submission")
async def build_submission(req: BuildSubmissionRequest) -> StreamingResponse:
    """Return the PSA or SGC CSV as a streamed file download."""
    with Session(get_engine()) as s:
        cards = [s.get(models.Card, cid) for cid in req.card_ids]
        missing = [cid for cid, c in zip(req.card_ids, cards) if c is None]
        if missing:
            raise HTTPException(404, f"card ids not found: {missing}")
        cards_found = [c for c in cards if c is not None]

    if req.service == "PSA":
        csv_text = grading_svc.build_psa_csv(cards_found, service_level=req.service_level)
        filename = f"psa_submission_{date.today().isoformat()}.csv"
    else:
        csv_text = grading_svc.build_sgc_csv(cards_found)
        filename = f"sgc_submission_{date.today().isoformat()}.csv"

    log.info("grading.build_submission", extra={
        "service": req.service, "card_count": len(cards_found)
    })

    return StreamingResponse(
        io.BytesIO(csv_text.encode("utf-8")),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ---------------------------------------------------------------------------
# POST /api/grading/queue
# ---------------------------------------------------------------------------

@router.post("/grading/queue", response_model=QueueResponse)
def queue_submission(req: QueueRequest) -> QueueResponse:
    """Mark cards as Pending Grading and assign a shared submission UUID."""
    submission_id = str(uuid.uuid4())
    today_str = date.today().isoformat()
    note_suffix = f"Sent for {req.service} {req.service_level} grading on {today_str}"

    queued = 0
    costs = grading_svc.PSA_COSTS if req.service == "PSA" else grading_svc.SGC_COSTS
    default_sl = "Value" if req.service == "PSA" else "Standard"
    cost_per_card = costs.get(req.service_level, costs.get(default_sl, 25.0))

    with session_scope() as s:
        for cid in req.card_ids:
            card = s.get(models.Card, cid)
            if card is None:
                continue
            card.status = "Pending Grading"
            card.grading_submission_id = submission_id
            # Append note without clobbering existing notes
            existing = card.notes or ""
            card.notes = (existing + "\n" + note_suffix).strip()
            card.updated_at = datetime.utcnow()
            s.add(card)
            queued += 1

    log.info("grading.queue", extra={
        "submission_id": submission_id,
        "service": req.service,
        "queued": queued,
    })

    return QueueResponse(
        submission_id=submission_id,
        queued=queued,
        estimated_cost=queued * cost_per_card,
    )


# ---------------------------------------------------------------------------
# GET /api/grading/queues
# ---------------------------------------------------------------------------

@router.get("/grading/queues", response_model=QueuesResponse)
def get_queues() -> QueuesResponse:
    """Return active grading submissions grouped by submission UUID."""
    with Session(get_engine()) as s:
        pending_cards = s.exec(
            select(models.Card).where(
                models.Card.status == "Pending Grading",
                models.Card.grading_submission_id.is_not(None),
            )
        ).all()

    # Group by submission_id
    groups: dict[str, list[models.Card]] = {}
    for card in pending_cards:
        sid = card.grading_submission_id
        groups.setdefault(sid, []).append(card)

    submissions = []
    for sid, cards in groups.items():
        # Try to infer service from notes
        service = None
        service_level = None
        for c in cards:
            if c.notes:
                for line in c.notes.split("\n"):
                    if "Sent for" in line:
                        parts = line.split()
                        # "Sent for PSA Value grading on ..."
                        if len(parts) >= 4:
                            service = parts[2]
                            service_level = parts[3]
                        break
            if service:
                break

        costs = grading_svc.PSA_COSTS if service == "PSA" else grading_svc.SGC_COSTS
        default_sl = "Value" if service == "PSA" else "Standard"
        cost_per = costs.get(service_level or default_sl, costs.get(default_sl, 25.0))

        submissions.append(SubmissionGroup(
            submission_id=sid,
            service=service,
            service_level=service_level,
            card_count=len(cards),
            card_ids=[c.id for c in cards],
            estimated_cost=len(cards) * cost_per,
        ))

    return QueuesResponse(submissions=submissions)


# ---------------------------------------------------------------------------
# POST /api/grading/{submission_id}/mark-back
# ---------------------------------------------------------------------------

@router.post("/grading/{submission_id}/mark-back", response_model=MarkBackResponse)
def mark_back(submission_id: str, req: MarkBackRequest) -> MarkBackResponse:
    """Record that cards returned from grading with assigned grades.

    Each card is set:
      is_graded=True, grade="PSA 9" (or just "9" if service not given),
      status="Researching" (so Connor can re-comp and re-list).
    """
    grade_prefix = f"{req.service} " if req.service else ""
    updated = 0

    with session_scope() as s:
        for item in req.items:
            card = s.get(models.Card, item.card_id)
            if card is None:
                continue
            # Only update if this card belongs to the submission
            if card.grading_submission_id != submission_id:
                continue
            card.is_graded = True
            card.grade = f"{grade_prefix}{item.grade}"
            card.status = "Researching"
            card.updated_at = datetime.utcnow()
            s.add(card)
            updated += 1

    log.info("grading.mark_back", extra={
        "submission_id": submission_id, "updated": updated
    })
    return MarkBackResponse(updated=updated)
