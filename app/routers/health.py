"""Expanded /api/health — checks each backend's reachability in parallel."""
from __future__ import annotations

import asyncio
from pathlib import Path

import httpx
from fastapi import APIRouter
from sqlmodel import select

from app import models, migrations
from app.config import settings
from app.db import get_engine, session
from app.services import sheets_sync, ebay_listing, claude_vision

router = APIRouter()


async def _check_db() -> dict:
    try:
        with session() as s:
            n = s.exec(select(models.HitWatchlistEntry)).all()
        return {"ok": True, "schema_version": migrations.current_version(get_engine()),
                "watchlist_entries": len(n)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def _check_google() -> dict:
    if not sheets_sync.is_connected():
        return {"ok": False, "connected": False, "reason": "no token"}
    try:
        # Confirm the token is refreshable / current — cheap call
        sheets_sync._load_credentials()
        return {"ok": True, "connected": True, "sheet_id": settings.google_sheet_id or None}
    except Exception as e:
        return {"ok": False, "connected": True, "error": str(e)}


async def _check_drive() -> dict:
    try:
        from app.services import drive_inbox
        f = drive_inbox.ensure_folders()
        files = drive_inbox.list_inbox(f)
        return {"ok": True, "inbox_count": len(files)}
    except Exception as e:
        return {"ok": False, "error": str(e)}


async def _check_ebay() -> dict:
    if not ebay_listing.is_configured():
        return {"ok": False, "configured": False, "env": settings.ebay_env}
    if not ebay_listing.is_connected():
        return {"ok": False, "configured": True, "connected": False, "env": settings.ebay_env}
    try:
        token = await ebay_listing._access_token()
        return {"ok": True, "configured": True, "connected": True,
                "env": settings.ebay_env, "token_prefix": token[:8] + "..."}
    except Exception as e:
        return {"ok": False, "configured": True, "connected": True,
                "error": str(e), "env": settings.ebay_env}


async def _check_claude() -> dict:
    """Report active backend (cli/http) and a basic reachability check."""
    backend = claude_vision.active_backend()
    cli_avail = claude_vision.cli_available()

    if backend == "cli":
        # Don't actually invoke claude — just confirm the binary resolves.
        return {"ok": cli_avail, "backend": "cli", "cli_available": cli_avail}

    # HTTP backend — confirm a key is set and api.anthropic.com is reachable
    if not settings.anthropic_api_key:
        return {"ok": False, "backend": "http", "key_present": False,
                "cli_available": cli_avail}
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            r = await client.get("https://api.anthropic.com/v1/messages",
                                 timeout=5.0)
            return {"ok": True, "backend": "http", "key_present": True,
                    "reachable_status": r.status_code, "cli_available": cli_avail}
    except Exception as e:
        return {"ok": False, "backend": "http", "key_present": True,
                "error": str(e), "cli_available": cli_avail}


@router.get("/health")
async def health() -> dict:
    db, google, drive, ebay, claude = await asyncio.gather(
        _check_db(), _check_google(), _check_drive(), _check_ebay(), _check_claude(),
        return_exceptions=False,
    )
    overall = db["ok"]  # DB has to work; everything else is "nice to have"
    return {
        "ok": overall,
        "version": "0.1.0",
        "ebay_env": settings.ebay_env,
        "checks": {
            "db": db,
            "google": google,
            "drive": drive,
            "ebay": ebay,
            "claude": claude,
        },
    }
