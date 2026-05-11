"""Manual sync triggers — re-push everything to Google Sheets / xlsx."""
from __future__ import annotations

from fastapi import APIRouter
from sqlmodel import Session, select

from app import models
from app.db import get_engine
from app.services import sheets_sync, xlsx_mirror

router = APIRouter()


@router.post("/sync/sheets")
def push_to_sheets() -> dict:
    pushed = 0; failed = 0
    with Session(get_engine()) as s:
        cards = s.exec(select(models.Card).order_by(models.Card.id)).all()
        for c in cards:
            ok = sheets_sync.append_card(c)
            pushed += int(ok); failed += int(not ok)
    return {"pushed": pushed, "failed": failed}


@router.post("/sync/xlsx")
def push_to_xlsx() -> dict:
    pushed = 0; failed = 0
    with Session(get_engine()) as s:
        cards = s.exec(select(models.Card).order_by(models.Card.id)).all()
        for c in cards:
            ok = xlsx_mirror.append_card(c)
            pushed += int(ok); failed += int(not ok)
    return {"pushed": pushed, "failed": failed}
