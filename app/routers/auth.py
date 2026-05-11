"""OAuth start / callback for Google Sheets and eBay."""
from __future__ import annotations

import urllib.parse
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.config import settings
from app.services import sheets_sync, ebay_listing

router = APIRouter()


@router.get("/auth/status")
def status() -> dict:
    return {
        "google": sheets_sync.is_connected(),
        "ebay_configured": ebay_listing.is_configured(),
        "ebay_connected": ebay_listing.is_connected(),
        "ebay_env": settings.ebay_env,
    }


# ---------------------------------------------------------------------------
# Google Sheets
# ---------------------------------------------------------------------------
@router.get("/auth/google/start")
def google_start(request: Request):
    redirect_uri = str(request.url_for("google_callback"))
    try:
        url, _ = sheets_sync.begin_auth(redirect_uri)
    except FileNotFoundError:
        raise HTTPException(412, "google_client_secret.json missing — see README OAuth setup.")
    return RedirectResponse(url)


@router.get("/auth/google/callback", name="google_callback")
def google_callback(request: Request, code: Optional[str] = None, error: Optional[str] = None):
    if error or not code:
        raise HTTPException(400, f"google oauth error: {error}")
    redirect_uri = str(request.url_for("google_callback"))
    sheets_sync.finish_auth(code, redirect_uri)
    return HTMLResponse(_close_window_html("Google Sheets connected."))


# ---------------------------------------------------------------------------
# eBay
# ---------------------------------------------------------------------------
@router.get("/auth/ebay/start")
def ebay_start():
    try:
        url = ebay_listing.begin_auth()
    except RuntimeError as e:
        raise HTTPException(412, str(e))
    return RedirectResponse(url)


@router.get("/auth/ebay/callback")
async def ebay_callback(code: Optional[str] = None, error: Optional[str] = None):
    if error or not code:
        raise HTTPException(400, f"ebay oauth error: {error}")
    await ebay_listing.finish_auth(code)
    return HTMLResponse(_close_window_html(f"eBay ({settings.ebay_env}) connected."))


def _close_window_html(msg: str) -> str:
    return (
        f"<html><body style='font-family:system-ui;padding:40px'>"
        f"<h2>✅ {msg}</h2><p>You can close this tab and return to CardScanner.</p>"
        f"<script>setTimeout(()=>window.close(), 1500)</script>"
        f"</body></html>"
    )
