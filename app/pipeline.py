"""Async batch pipeline: image upload → identify → comp lookup → save → mirror.

Runs as a background asyncio task. Progress is reported via the ScanJob row in
the DB; the front-end polls /scans/jobs/{id}.
"""
from __future__ import annotations

import asyncio
import json
import shutil
from datetime import datetime, date
from pathlib import Path
from typing import Optional

import httpx
from sqlmodel import Session, select

from app import models, achievements
from app.config import settings, UPLOAD_DIR
from app.db import get_engine, session_scope
from app.services import claude_vision, comp_lookup, hit_watchlist, sheets_sync, xlsx_mirror, drive_inbox
from app.utils import logger as _log
from app.utils.images import normalize as normalize_image

log = _log.get(__name__)


async def _process_one(
    image_path: Path,
    api_key_override: Optional[str],
    *,
    back_path: Optional[Path] = None,
    front_hash: Optional[str] = None,
    back_hash: Optional[str] = None,
    drive_front_id: Optional[str] = None,
    drive_back_id: Optional[str] = None,
) -> dict:
    """Identify + comp-lookup a single image. Persist a Card. Return dict."""
    started = datetime.utcnow()
    # Normalize HEIC etc. up-front (also recompute hash post-normalize to keep
    # idempotency stable across "raw HEIC vs converted JPEG of same image").
    image_path = normalize_image(image_path)
    if back_path:
        back_path = normalize_image(back_path)

    # Idempotency check (only when we know the hash already)
    if front_hash:
        with session_scope() as s:
            from sqlmodel import select as _select
            existing = s.exec(_select(models.Card).where(
                (models.Card.front_hash == front_hash) | (models.Card.back_hash == front_hash)
            )).first()
            if existing:
                log.info("dedupe skip", extra={"hash": front_hash[:12], "card_id": existing.id})
                return {"ok": True, "skipped": True, "card_id": existing.id}

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            with _log.step(log, "claude_identify", front=image_path.name):
                ident = await claude_vision.identify_card_async(
                    str(image_path),
                    back_path=str(back_path) if back_path else None,
                    api_key_override=api_key_override, client=client,
                )
        with _log.step(log, "comp_lookup", player=ident.player, year=ident.year):
            comp = await comp_lookup.fetch_comps(
                ident.year, ident.set_brand, ident.player, ident.card_no, ident.parallel,
            )

        with session_scope() as s:
            # A4: build notes, incorporating low-confidence flag if needed
            card_notes = ident.notes
            review_flagged = ident.review_flagged
            if ident.low_confidence_fields:
                flag_note = f"Auto-flagged: low confidence on {ident.low_confidence_fields}"
                card_notes = f"{card_notes}\n{flag_note}" if card_notes else flag_note
                review_flagged = True
            # B3: append bulk-lot warning to notes
            if comp.suspicious_bulk:
                bulk_note = "⚠️ Comp prices look like a bulk-lot fingerprint — verify before pricing"
                card_notes = f"{card_notes}\n{bulk_note}" if card_notes else bulk_note
            # B3: effective value prefers recency-weighted median
            effective_value = comp.median_recency_weighted or comp.median

            card = models.Card(
                year=ident.year, set_brand=ident.set_brand, player=ident.player,
                card_no=ident.card_no, parallel=ident.parallel or "Base",
                team=ident.team, condition=ident.condition,
                is_graded=ident.is_graded, grade=ident.grade,
                is_autograph=ident.is_autograph, is_relic=ident.is_relic,
                review_flagged=review_flagged,
                front_image=image_path.name,
                back_image=back_path.name if back_path else None,
                front_hash=front_hash, back_hash=back_hash,
                drive_front_id=drive_front_id, drive_back_id=drive_back_id,
                comp_median=comp.median, comp_low=comp.low, comp_high=comp.high,
                comp_count=comp.count, comp_url=comp.url,
                comp_fetched_at=datetime.utcnow(),
                est_value_raw=effective_value,
                notes=card_notes,
                consider_grading=(effective_value or 0) >= 30 and not ident.is_graded,
                # A4: vision intelligence fields
                is_rookie=ident.is_rookie,
                is_serial_numbered=ident.is_serial_numbered,
                serial_print_run=ident.serial_print_run,
                photo_quality=ident.photo_quality,
                low_confidence_fields=(
                    json.dumps(ident.low_confidence_fields)
                    if ident.low_confidence_fields else None
                ),
                condition_signals=(
                    json.dumps(ident.condition_signals)
                    if ident.condition_signals else None
                ),
                # B3: comp intelligence fields
                comp_confidence=comp.confidence,
                comp_median_weighted=comp.median_recency_weighted,
                comp_suspicious_bulk=comp.suspicious_bulk,
                comp_suspicious_reason=comp.suspicious_reason or None,
            )
            is_hit, reason = hit_watchlist.match(card, s)
            card.is_hit_watchlist = is_hit
            card.hit_reason = reason
            s.add(card); s.commit(); s.refresh(card)

            achievements.record_daily_activity(s)
            new_unlocks = achievements.evaluate_unlocks(s, just_added_card=card)

            # Snapshot everything we need OUTSIDE the session
            snapshot = {
                "ok": True,
                "card_id": card.id,
                "title": card.display_title(),
                "value": card.comp_median_weighted or card.comp_median or 0,
                "hit": card.is_hit_watchlist,
                "unlocks": [u.code for u in new_unlocks],
            }
            # Mirror writes (best-effort) inside the session so the ORM object is alive
            try:
                xlsx_mirror.append_card(card)
            except Exception:
                pass
            try:
                sheets_sync.append_card(card)
            except Exception:
                pass

        log.info("card processed", extra={
            "card_id": snapshot["card_id"], "value": snapshot["value"],
            "hit": snapshot["hit"], "duration_ms": int((datetime.utcnow()-started).total_seconds()*1000),
        })
        return snapshot
    except Exception as e:
        log.exception("card process fail", extra={
            "image": image_path.name, "error": str(e),
            "duration_ms": int((datetime.utcnow()-started).total_seconds()*1000),
        })
        return {"ok": False, "error": str(e), "image": str(image_path.name)}


async def run_job(job_id: int, image_paths: list[Path], api_key_override: Optional[str]) -> None:
    """Process all images for a job, updating progress as we go."""
    with session_scope() as s:
        job = s.get(models.ScanJob, job_id)
        job.status = "processing"
        job.started_at = datetime.utcnow()
        s.add(job)

    sem = asyncio.Semaphore(3)  # cap concurrent Claude calls

    async def worker(p: Path) -> dict:
        async with sem:
            res = await _process_one(p, api_key_override)
            with session_scope() as s:
                job = s.get(models.ScanJob, job_id)
                job.processed += 1
                if not res.get("ok"):
                    job.failed += 1
                s.add(job)
            return res

    await asyncio.gather(*(worker(p) for p in image_paths))

    with session_scope() as s:
        job = s.get(models.ScanJob, job_id)
        job.status = "done"
        job.finished_at = datetime.utcnow()
        s.add(job)


# ---------------------------------------------------------------------------
# Drive-driven sync
# ---------------------------------------------------------------------------
async def run_drive_sync(api_key_override: Optional[str] = None) -> int:
    """Scan the Drive inbox, process new files, return the job_id."""
    folders = drive_inbox.ensure_folders()
    files = drive_inbox.list_inbox(folders)
    pairs = drive_inbox.pair_files(files)

    with session_scope() as s:
        job = models.ScanJob(label=f"Drive sync ({len(pairs)} cards)", total=len(pairs))
        s.add(job); s.commit(); s.refresh(job)
        job_id = job.id

    if not pairs:
        with session_scope() as s:
            j = s.get(models.ScanJob, job_id)
            j.status = "done"; j.finished_at = datetime.utcnow()
            s.add(j)
        return job_id

    asyncio.create_task(_run_drive_job(job_id, pairs, folders, api_key_override))
    return job_id


async def _run_drive_job(job_id: int, pairs, folders, api_key_override) -> None:
    with session_scope() as s:
        j = s.get(models.ScanJob, job_id)
        j.status = "processing"; j.started_at = datetime.utcnow()
        s.add(j)

    sem = asyncio.Semaphore(3)

    async def worker(front, back):
        async with sem:
            try:
                # Download
                front_path = UPLOAD_DIR / front["name"]
                drive_inbox.download_to(front_path, front["id"])
                fh = drive_inbox.hash_file(front_path)

                if drive_inbox.already_have_hash(fh):
                    drive_inbox.move_file(front["id"], folders.processed_id)
                    if back:
                        drive_inbox.move_file(back["id"], folders.processed_id)
                    return {"ok": True, "skipped": True, "name": front["name"]}

                back_path = None; bh = None
                if back:
                    back_path = UPLOAD_DIR / back["name"]
                    drive_inbox.download_to(back_path, back["id"])
                    bh = drive_inbox.hash_file(back_path)

                res = await _process_one(
                    front_path, api_key_override,
                    back_path=back_path, front_hash=fh, back_hash=bh,
                    drive_front_id=front["id"], drive_back_id=back["id"] if back else None,
                )
                # Move
                target = folders.processed_id if res.get("ok") else folders.failed_id
                drive_inbox.move_file(front["id"], target)
                if back:
                    drive_inbox.move_file(back["id"], target)
                return res
            except Exception as e:
                if front:
                    try: drive_inbox.move_file(front["id"], folders.failed_id)
                    except Exception: pass
                if back:
                    try: drive_inbox.move_file(back["id"], folders.failed_id)
                    except Exception: pass
                return {"ok": False, "error": str(e)}
            finally:
                with session_scope() as s:
                    j = s.get(models.ScanJob, job_id)
                    j.processed += 1
                    s.add(j)

    await asyncio.gather(*(worker(f, b) for f, b in pairs))

    with session_scope() as s:
        j = s.get(models.ScanJob, job_id)
        j.status = "done"; j.finished_at = datetime.utcnow()
        s.add(j)
