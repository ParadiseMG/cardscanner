"""A6: Tests for batch resume logic (A4)."""
from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch, AsyncMock

import pytest
from sqlmodel import Session

from app import models
from app.db import get_engine, session_scope


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_job(**kwargs) -> int:
    """Create a ScanJob and return its id."""
    defaults = dict(
        status="processing",
        total=4,
        processed=2,
        started_at=datetime.utcnow(),
        finished_at=None,
        source_manifest=None,
    )
    defaults.update(kwargs)
    with Session(get_engine()) as s:
        job = models.ScanJob(**defaults)
        s.add(job)
        s.commit()
        return job.id


def _get_job(jid: int) -> dict:
    """Return job attributes as a plain dict to avoid DetachedInstanceError."""
    with Session(get_engine()) as s:
        j = s.get(models.ScanJob, jid)
        if j is None:
            return {}
        return {
            "id": j.id,
            "status": j.status,
            "finished_at": j.finished_at,
            "source_manifest": j.source_manifest,
        }


def _make_card(job_id: int, front_hash: str, player: str = "X") -> int:
    """Create a Card and return its id."""
    with Session(get_engine()) as s:
        c = models.Card(
            player=player, status="Researching",
            scan_job_id=job_id, front_hash=front_hash,
        )
        s.add(c)
        s.commit()
        return c.id


# ---------------------------------------------------------------------------
# Import the resume function directly so we can test it in isolation
# ---------------------------------------------------------------------------

def _call_resume():
    """Import and run _resume_stranded_jobs (avoids full create_app)."""
    from app.main import _resume_stranded_jobs
    _resume_stranded_jobs()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_old_job_marked_abandoned():
    """A job started > 60 min ago should be marked 'abandoned'."""
    old_start = datetime.utcnow() - timedelta(minutes=90)
    jid = _make_job(status="processing", started_at=old_start)
    _call_resume()
    j = _get_job(jid)
    assert j["status"] == "abandoned"
    assert j["finished_at"] is not None


def test_recent_job_no_manifest_abandoned():
    """A recent job with no manifest can't be resumed — should be abandoned."""
    jid = _make_job(status="processing", started_at=datetime.utcnow(), source_manifest=None)
    _call_resume()
    j = _get_job(jid)
    assert j["status"] == "abandoned"


def test_recent_job_all_done_marked_complete():
    """If all manifest items already have cards, job should be marked done."""
    manifest = [
        {"path": "/tmp/img_a.jpg", "front_hash": "hash_a"},
        {"path": "/tmp/img_b.jpg", "front_hash": "hash_b"},
    ]
    jid = _make_job(
        status="processing",
        started_at=datetime.utcnow(),
        source_manifest=json.dumps(manifest),
    )
    # Both hashes already have cards
    _make_card(jid, "hash_a", "Player A")
    _make_card(jid, "hash_b", "Player B")

    _call_resume()

    j = _get_job(jid)
    assert j["status"] == "done"
    assert j["finished_at"] is not None


def test_recent_job_partial_resumes():
    """If only some manifest items are done, resume should kick off remaining."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create two real temp image files (pipeline needs Path objects to exist)
        img_a = Path(tmpdir) / "img_a.jpg"
        img_b = Path(tmpdir) / "img_b.jpg"
        img_a.write_bytes(b"FAKEJPEG_A")
        img_b.write_bytes(b"FAKEJPEG_B")

        manifest = [
            {"path": str(img_a), "front_hash": "hash_x"},
            {"path": str(img_b), "front_hash": "hash_y"},
        ]
        jid = _make_job(
            status="processing",
            started_at=datetime.utcnow(),
            source_manifest=json.dumps(manifest),
        )
        # Only hash_x is done
        _make_card(jid, "hash_x", "Done Player")

        # Mock pipeline.run_job so we can verify it was called with remaining items
        from app import pipeline
        run_job_calls = []
        async def fake_run_job(job_id, paths, api_key_override):
            run_job_calls.append((job_id, list(paths)))

        with patch.object(pipeline, "run_job", new=fake_run_job):
            # We need a running loop for create_task to work
            async def _run():
                _call_resume()
                # Give the event loop a tick to process created tasks
                await asyncio.sleep(0.05)

            asyncio.run(_run())

        # run_job should have been called with only img_b (hash_y not done)
        assert len(run_job_calls) == 1
        called_jid, called_paths = run_job_calls[0]
        assert called_jid == jid
        assert len(called_paths) == 1
        assert called_paths[0] == img_b


def test_queued_job_old_abandoned():
    """A queued job (never started) older than 60 min should be abandoned."""
    old_start = datetime.utcnow() - timedelta(minutes=120)
    jid = _make_job(status="queued", started_at=old_start)
    _call_resume()
    j = _get_job(jid)
    assert j["status"] == "abandoned"


def test_done_job_not_touched():
    """A job that's already 'done' should not be modified."""
    jid = _make_job(status="done", started_at=datetime.utcnow())
    _call_resume()
    j = _get_job(jid)
    assert j["status"] == "done"  # unchanged
