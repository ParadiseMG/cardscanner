"""CRUD for named storage locations (binders, boxes, toploader cases…).

Names are case-insensitively unique. The DB column is `COLLATE NOCASE`, but we
also normalize whitespace at the API layer so 'Box  A' (two spaces) collapses
to 'Box A'. The POST endpoint is idempotent: submitting a name that already
exists (any case variation) returns the existing row instead of erroring —
this is what the UI wants when the user types into a free-text + create-new
field.

Delete is protected by default: if cards reference a location, the request
returns 409 with the count. Pass `?force=true` to detach them and proceed.
"""
from __future__ import annotations

from typing import Optional, List
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlmodel import Session, select
from sqlalchemy import func

from app import models
from app.db import get_engine

router = APIRouter()


class StorageLocationIn(BaseModel):
    name: str
    kind: Optional[str] = "other"
    notes: Optional[str] = None


class StorageLocationPatch(BaseModel):
    name: Optional[str] = None
    kind: Optional[str] = None
    notes: Optional[str] = None


class StorageLocationOut(BaseModel):
    id: int
    name: str
    kind: str
    notes: Optional[str] = None
    card_count: int
    created_at: str

    @classmethod
    def from_db(cls, loc: "models.StorageLocation", card_count: int) -> "StorageLocationOut":
        return cls(
            id=loc.id, name=loc.name, kind=loc.kind, notes=loc.notes,
            card_count=card_count,
            created_at=loc.created_at.isoformat() if loc.created_at else "",
        )


def _find_case_insensitive(s: Session, name: str) -> Optional[models.StorageLocation]:
    """Look up a location by normalized name, case-insensitive."""
    stmt = select(models.StorageLocation).where(
        func.lower(models.StorageLocation.name) == name.lower()
    )
    return s.exec(stmt).first()


def _count_cards(s: Session, location_id: int) -> int:
    return s.exec(
        select(func.count(models.Card.id))
        .where(models.Card.storage_location_id == location_id)
    ).one()


@router.get("/storage-locations")
def list_locations() -> List[dict]:
    with Session(get_engine()) as s:
        locs = s.exec(
            select(models.StorageLocation).order_by(models.StorageLocation.name)
        ).all()
        return [
            StorageLocationOut.from_db(loc, _count_cards(s, loc.id)).model_dump()
            for loc in locs
        ]


@router.post("/storage-locations", status_code=201)
def create_location(payload: StorageLocationIn) -> dict:
    name = models.normalize_location_name(payload.name)
    if not name:
        raise HTTPException(400, "name is required")
    kind = (payload.kind or "other").strip().lower() or "other"
    with Session(get_engine()) as s:
        existing = _find_case_insensitive(s, name)
        if existing:
            # Idempotent — return the existing row so the UI's "create or pick"
            # flow doesn't have to special-case duplicates.
            return StorageLocationOut.from_db(
                existing, _count_cards(s, existing.id)
            ).model_dump()
        loc = models.StorageLocation(name=name, kind=kind, notes=payload.notes)
        s.add(loc); s.commit(); s.refresh(loc)
        return StorageLocationOut.from_db(loc, 0).model_dump()


@router.patch("/storage-locations/{loc_id}")
def patch_location(loc_id: int, patch: StorageLocationPatch) -> dict:
    with Session(get_engine()) as s:
        loc = s.get(models.StorageLocation, loc_id)
        if not loc:
            raise HTTPException(404, "storage location not found")
        if patch.name is not None:
            new_name = models.normalize_location_name(patch.name)
            if not new_name:
                raise HTTPException(400, "name cannot be empty")
            # If a *different* row already holds this name (any case), reject.
            clash = _find_case_insensitive(s, new_name)
            if clash and clash.id != loc.id:
                raise HTTPException(
                    409, f"another location already uses that name (id={clash.id})"
                )
            loc.name = new_name
        if patch.kind is not None:
            loc.kind = patch.kind.strip().lower() or "other"
        if patch.notes is not None:
            loc.notes = patch.notes
        s.add(loc); s.commit(); s.refresh(loc)
        return StorageLocationOut.from_db(loc, _count_cards(s, loc.id)).model_dump()


@router.delete("/storage-locations/{loc_id}")
def delete_location(loc_id: int, force: bool = Query(False)) -> dict:
    with Session(get_engine()) as s:
        loc = s.get(models.StorageLocation, loc_id)
        if not loc:
            raise HTTPException(404, "storage location not found")
        count = _count_cards(s, loc_id)
        if count and not force:
            raise HTTPException(
                409,
                f"{count} card(s) reference this location; "
                f"pass ?force=true to detach and delete",
            )
        if count:
            # Detach referenced cards before delete.
            cards = s.exec(
                select(models.Card).where(models.Card.storage_location_id == loc_id)
            ).all()
            now = datetime.utcnow()
            for c in cards:
                c.storage_location_id = None
                c.updated_at = now
                s.add(c)
        s.delete(loc); s.commit()
        return {"deleted": loc_id, "detached_cards": count}
