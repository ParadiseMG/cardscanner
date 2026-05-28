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
from app.services import claude_vision, comp_lookup, hit_watchlist, sheets_sync, xlsx_mirror
from app.services.auto_pair import auto_pair
from app.utils import logger as _log
from app.utils.images import normalize as normalize_image

log = _log.get(__name__)


def _record_failure(job_id: Optional[int], file_name: str, error: str,
                    *, error_class: Optional[str] = None) -> None:
    """Persist one failure row so the dashboard can show the user why it failed."""
    try:
        with session_scope() as s:
            s.add(models.JobFailure(
                scan_job_id=job_id, file_name=file_name,
                error=error[:500], error_class=error_class,
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
    job_batch_year: Optional[int] = None
    job_batch_set_brand: Optional[str] = None
    if job_id is not None:
        with session_scope() as s:
            j = s.get(models.ScanJob, job_id)
            if j is not None:
                job_storage_location_id = j.storage_location_id
                job_storage_position = j.storage_position
                job_batch_year = j.batch_year
                job_batch_set_brand = j.batch_set_brand

    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            with _log.step(log, "claude_identify", front=image_path.name):
                ident = await claude_vision.identify_card_async(
                    str(image_path),
                    back_path=str(back_path) if back_path else None,
                    api_key_override=api_key_override, client=client,
                )

        # Override year/set_brand with user-provided batch values
        if job_batch_year is not None:
            ident.year = job_batch_year
            ident.field_confidence["year"] = 1.0
        if job_batch_set_brand:
            ident.set_brand = job_batch_set_brand
            ident.field_confidence["set_brand"] = 1.0

        # Comp lookup is best-effort — save the card even if pricing fails
        comp = None
        if settings.skip_comp_lookup:
            log.info("comp lookup skipped (SKIP_COMP_LOOKUP=true)",
                     extra={"player": ident.player})
        else:
            try:
                with _log.step(log, "comp_lookup", player=ident.player, year=ident.year):
                    comp = await comp_lookup.fetch_comps(
                        ident.year, ident.set_brand, ident.player, ident.card_no, ident.parallel,
                    )
            except Exception as comp_err:
                log.warning("comp lookup failed, saving card without pricing",
                            extra={"player": ident.player, "error": str(comp_err)})

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
            if comp and comp.suspicious_bulk:
                bulk_note = "⚠️ Comp prices look like a bulk-lot fingerprint — verify before pricing"
                card_notes = f"{card_notes}\n{bulk_note}" if card_notes else bulk_note
            effective_value = None
            if comp:
                effective_value = comp.median_recency_weighted or comp.median
            if comp is None:
                price_note = "⏳ Pricing pending — comp lookup failed, retry from inventory"
                card_notes = f"{card_notes}\n{price_note}" if card_notes else price_note
                review_flagged = True

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
                comp_median=comp.median if comp else None,
                comp_low=comp.low if comp else None,
                comp_high=comp.high if comp else None,
                comp_count=comp.count if comp else 0,
                comp_url=comp.url if comp else None,
                comp_fetched_at=datetime.utcnow() if comp else None,
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
                comp_confidence=comp.confidence if comp else "none",
                comp_median_weighted=comp.median_recency_weighted if comp else None,
                comp_suspicious_bulk=comp.suspicious_bulk if comp else False,
                comp_suspicious_reason=(comp.suspicious_reason or None) if comp else None,
                # Link card to its parent scan job for re-identification
                scan_job_id=job_id,
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
            "hit": snapshot["hit"], "priced": comp is not None,
            "duration_ms": int((datetime.utcnow()-started).total_seconds()*1000),
        })
        return snapshot
    except Exception as e:
        log.exception("card process fail", extra={
            "image": image_path.name, "error": str(e),
            "duration_ms": int((datetime.utcnow()-started).total_seconds()*1000),
        })
        _record_failure(job_id, image_path.name, str(e),
                        error_class=type(e).__name__)
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
async def run_job_paired(job_id: int, pairs: list, api_key_override: Optional[str]) -> None:
    """Process pre-paired images (from video extraction). Skips auto_pair."""
    import shutil as _shutil
    from app.services.auto_pair import Pair
    from app.services.video_pair import PairedCard

    # Copy frame images to UPLOAD_DIR so they persist for re-identification
    pipeline_pairs = []
    for p in pairs:
        front_dest = UPLOAD_DIR / p.front.name
        if not front_dest.exists():
            _shutil.copy2(p.front, front_dest)
        back_dest = None
        if p.back:
            back_dest = UPLOAD_DIR / p.back.name
            if not back_dest.exists():
                _shutil.copy2(p.back, back_dest)
        pipeline_pairs.append(Pair(front=front_dest, back=back_dest))

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
