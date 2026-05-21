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
from app.services.auto_pair import auto_pair
from app.utils import logger as _log
from app.utils.images import normalize as normalize_image

log = _log.get(__name__)


def _record_failure(job_id: Optional[int], file_name: str, error: str,
                    *, error_class: Optional[str] = None,
                    drive_id: Optional[str] = None) -> None:
    """Persist one failure row so the dashboard can show the user why it failed."""
    try:
        with session_scope() as s:
            s.add(models.JobFailure(
                scan_job_id=job_id, file_name=file_name,
                drive_id=drive_id, error=error[:500], error_class=error_class,
            ))
    except Exception:
        log.exception("failed to record JobFailure")


# Track the current job_id in a contextvar-ish module global for child tasks
_CURRENT_JOB_ID: dict[str, int] = {}


async def _process_one(
    image_path: Path,
    api_key_override: Optional[str],
    *,
    back_path: Optional[Path] = None,
    front_hash: Optional[str] = None,
    back_hash: Optional[str] = None,
    drive_front_id: Optional[str] = None,
    drive_back_id: Optional[str] = None,
    job_id: Optional[int] = None,
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

    # m11: if the parent ScanJob carries a storage tag, every Card from this
    # job inherits it. Read once before the long-running network calls so a
    # restart still picks it up (it's persisted on the job row).
    job_storage_location_id: Optional[int] = None
    job_storage_position: Optional[str] = None
    if job_id is not None:
        with session_scope() as s:
            j = s.get(models.ScanJob, job_id)
            if j is not None:
                job_storage_location_id = j.storage_location_id
                job_storage_position = j.storage_position

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

        # Reconcile sport: trust set_brand keywords over Claude's default-Baseball bias.
        from app.utils.sport_inference import reconcile_sport
        ident_sport = reconcile_sport(
            ident.sport, ident.set_brand, ident.player, ident.team,
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
                sport=ident_sport,
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
                # m11: inherit storage from the parent sync job
                storage_location_id=job_storage_location_id,
                storage_position=job_storage_position,
            )
            is_hit, reason = hit_watchlist.match(card, s)
            card.is_hit_watchlist = is_hit
            card.hit_reason = reason
            # Flag cards above threshold for photo retake (useful for video-sourced scans)
            if card.est_value_raw and card.est_value_raw >= settings.video_retake_photo_threshold:
                card.needs_photo_verification = True
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
        _record_failure(job_id, image_path.name, str(e),
                        error_class=type(e).__name__,
                        drive_id=drive_front_id)
        return {"ok": False, "error": str(e), "image": str(image_path.name)}


async def run_job(job_id: int, image_paths: list[Path], api_key_override: Optional[str]) -> None:
    """Process all images for a job, updating progress as we go.

    Photos are auto-paired by EXIF capture time (front+back of the same card
    snapped within 15s) with a visual-similarity sanity check, so the user
    doesn't have to rename files. Each Pair becomes one Card.
    """
    pairs = auto_pair(image_paths)

    with session_scope() as s:
        job = s.get(models.ScanJob, job_id)
        job.status = "processing"
        job.started_at = datetime.utcnow()
        job.total = len(pairs)  # one row per Pair, not per file
        s.add(job)

    sem = asyncio.Semaphore(3)  # cap concurrent Claude calls

    async def worker(pair) -> dict:
        async with sem:
            res = await _process_one(pair.front, api_key_override,
                                      back_path=pair.back, job_id=job_id)
            with session_scope() as s:
                job = s.get(models.ScanJob, job_id)
                job.processed += 1
                if not res.get("ok"):
                    job.failed += 1
                s.add(job)
            return res

    await asyncio.gather(*(worker(p) for p in pairs))

    with session_scope() as s:
        job = s.get(models.ScanJob, job_id)
        job.status = "done"
        job.finished_at = datetime.utcnow()
        s.add(job)


# ---------------------------------------------------------------------------
# Drive-driven sync
# ---------------------------------------------------------------------------
async def run_drive_sync(api_key_override: Optional[str] = None,
                         *,
                         storage_location_id: Optional[int] = None,
                         storage_position: Optional[str] = None) -> int:
    """Scan the Drive inbox, process new files, return the job_id.

    Refuses to start a second concurrent Drive sync (prevents the double-job
    race that doubles processing cost). Returns the existing job_id if one
    is already in flight.

    If `storage_location_id` is provided, every Card the job creates inherits
    it (and the optional `storage_position`). Stored on the ScanJob row so
    the tag survives a mid-sync restart.
    """
    # Validate the location reference first — a bad caller should always get
    # 400, even when another sync is already in flight.
    if storage_location_id is not None:
        with session_scope() as s:
            if not s.get(models.StorageLocation, storage_location_id):
                raise ValueError(
                    f"storage_location_id={storage_location_id} does not exist"
                )

    # Duplicate-job lock: refuse if another Drive sync is already running.
    with session_scope() as s:
        from sqlmodel import select as _sel
        in_flight = s.exec(
            _sel(models.ScanJob).where(
                models.ScanJob.status.in_(["queued", "processing"]),
                models.ScanJob.label.like("Drive sync%"),
            )
        ).first()
        if in_flight:
            log.info("drive_sync skipped: existing job in flight",
                     extra={"existing_job_id": in_flight.id})
            return in_flight.id

    folders = drive_inbox.ensure_folders()
    files = drive_inbox.list_inbox(folders)
    # Initial filename-based pairing (just to estimate `total`; real pairing
    # happens post-download in _run_drive_job).
    initial_pairs = drive_inbox.pair_files(files)

    with session_scope() as s:
        job = models.ScanJob(label=f"Drive sync ({len(files)} files)",
                             total=len(initial_pairs),
                             storage_location_id=storage_location_id,
                             storage_position=storage_position)
        s.add(job); s.commit(); s.refresh(job)
        job_id = job.id

    if not files:
        with session_scope() as s:
            j = s.get(models.ScanJob, job_id)
            j.status = "done"; j.finished_at = datetime.utcnow()
            s.add(j)
        return job_id

    asyncio.create_task(_run_drive_job(job_id, files, initial_pairs, folders, api_key_override))
    return job_id


async def _run_drive_job(job_id: int, all_files, initial_pairs, folders, api_key_override) -> None:
    with session_scope() as s:
        j = s.get(models.ScanJob, job_id)
        j.status = "processing"; j.started_at = datetime.utcnow()
        s.add(j)

    # ------------------------------------------------------------------
    # Phase 1: download every file and remember its Drive metadata
    # ------------------------------------------------------------------
    file_meta_by_path: dict[Path, dict] = {}
    explicit_pair_paths: set[Path] = set()
    for f in all_files:
        local = UPLOAD_DIR / f["name"]
        try:
            drive_inbox.download_to(local, f["id"])
        except Exception as e:
            log.warning("drive download failed",
                        extra={"name": f["name"], "error": type(e).__name__})
            continue
        file_meta_by_path[local] = f

    # Filename-suffix pairs from the initial pass — preserve those choices
    explicit_pairs_local: list = []
    for front_meta, back_meta in initial_pairs:
        if front_meta and back_meta:
            fp = UPLOAD_DIR / front_meta["name"]
            bp = UPLOAD_DIR / back_meta["name"]
            if fp in file_meta_by_path and bp in file_meta_by_path:
                from app.services.auto_pair import Pair
                explicit_pairs_local.append(Pair(front=fp, back=bp))
                explicit_pair_paths.add(fp)
                explicit_pair_paths.add(bp)

    # Auto-pair the rest by EXIF time + visual verification
    leftover = [p for p in file_meta_by_path if p not in explicit_pair_paths]
    inferred_pairs = auto_pair(leftover)

    final_pairs = explicit_pairs_local + inferred_pairs

    # Update job total to reflect the real pair count (auto-pairing may
    # have collapsed singletons into pairs)
    with session_scope() as s:
        j = s.get(models.ScanJob, job_id)
        j.total = len(final_pairs)
        s.add(j)

    sem = asyncio.Semaphore(3)

    async def worker(pair):
        front_path = pair.front
        back_path = pair.back
        front = file_meta_by_path.get(front_path)
        back = file_meta_by_path.get(back_path) if back_path else None
        async with sem:
            try:
                fh = drive_inbox.hash_file(front_path)

                if drive_inbox.already_have_hash(fh):
                    if front: drive_inbox.move_file(front["id"], folders.processed_id)
                    if back: drive_inbox.move_file(back["id"], folders.processed_id)
                    return {"ok": True, "skipped": True, "name": front_path.name}

                bh = drive_inbox.hash_file(back_path) if back_path else None

                res = await _process_one(
                    front_path, api_key_override,
                    back_path=back_path, front_hash=fh, back_hash=bh,
                    drive_front_id=front["id"] if front else None,
                    drive_back_id=back["id"] if back else None,
                    job_id=job_id,
                )
                # Move
                target = folders.processed_id if res.get("ok") else folders.failed_id
                if front: drive_inbox.move_file(front["id"], target)
                if back: drive_inbox.move_file(back["id"], target)
                return res
            except Exception as e:
                _record_failure(job_id, front_path.name, str(e),
                                error_class=type(e).__name__,
                                drive_id=front["id"] if front else None)
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

    await asyncio.gather(*(worker(p) for p in final_pairs))

    with session_scope() as s:
        j = s.get(models.ScanJob, job_id)
        j.status = "done"; j.finished_at = datetime.utcnow()
        s.add(j)


async def run_job_paired(job_id: int, pairs: list, api_key_override: Optional[str]) -> None:
    """Process pre-paired images (from video extraction). Skips auto_pair."""
    from app.services.auto_pair import Pair
    from app.services.video_pair import PairedCard

    # Convert PairedCard to Pair for the worker
    pipeline_pairs = [Pair(front=p.front, back=p.back) for p in pairs]

    with session_scope() as s:
        job = s.get(models.ScanJob, job_id)
        job.status = "processing"
        job.started_at = datetime.utcnow()
        job.total = len(pipeline_pairs)
        s.add(job)

    # Ollama can only handle 1 vision request at a time (VRAM limit),
    # so process cards sequentially to avoid 500 errors.
    for pair in pipeline_pairs:
        res = await _process_one(pair.front, api_key_override,
                                  back_path=pair.back, job_id=job_id)
        with session_scope() as s:
            job = s.get(models.ScanJob, job_id)
            job.processed += 1
            if not res.get("ok"):
                job.failed += 1
            s.add(job)

    with session_scope() as s:
        job = s.get(models.ScanJob, job_id)
        job.status = "done"
        job.finished_at = datetime.utcnow()
        s.add(job)
