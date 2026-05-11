"""Google Sheets OAuth + append-row helpers.

OAuth flow
----------
We use the standard installed-app flow (`InstalledAppFlow`) from
`google-auth-oauthlib`. On first run the user clicks an `/auth/google/start`
link in the dashboard; the local server hosts a one-shot callback at
`/auth/google/callback`. The refresh token persists at
`data/google_token.json` so subsequent boots are silent.

If GOOGLE_SHEET_ID is empty we create a new spreadsheet with the
"CardScanner Inventory" name and return the new ID for the user to save in
.env.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.config import settings
from app import models


SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
SHEET_HEADER = [
    "Year", "Set/Brand", "Player", "Card #", "Parallel", "Condition",
    "Est Value Raw", "Comp Median", "Comp Low", "Comp High", "Comps URL",
    "Status", "Channel", "eBay Status", "eBay Listing ID", "Hit?", "Notes",
    "Cataloged At",
]


def _load_credentials() -> Optional[Credentials]:
    p = Path(settings.google_token_path)
    if not p.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(p), SCOPES)
    if not creds.valid and creds.refresh_token:
        from google.auth.transport.requests import Request
        creds.refresh(Request())
        p.write_text(creds.to_json())
    return creds


def _save_credentials(creds: Credentials) -> None:
    Path(settings.google_token_path).write_text(creds.to_json())


def begin_auth(redirect_uri: str) -> tuple[str, str]:
    """Build an authorization URL + state token; return both."""
    flow = Flow.from_client_secrets_file(
        settings.google_oauth_client_secrets, scopes=SCOPES,
        redirect_uri=redirect_uri,
    )
    auth_url, state = flow.authorization_url(
        access_type="offline", include_granted_scopes="true", prompt="consent",
    )
    return auth_url, state


def finish_auth(code: str, redirect_uri: str) -> Credentials:
    flow = Flow.from_client_secrets_file(
        settings.google_oauth_client_secrets, scopes=SCOPES,
        redirect_uri=redirect_uri,
    )
    flow.fetch_token(code=code)
    _save_credentials(flow.credentials)
    return flow.credentials


def _service():
    creds = _load_credentials()
    if not creds:
        raise RuntimeError("Google Sheets not connected — start /auth/google/start first.")
    return build("sheets", "v4", credentials=creds, cache_discovery=False)


def ensure_sheet() -> str:
    """Return the spreadsheet ID, creating one if necessary."""
    if settings.google_sheet_id:
        return settings.google_sheet_id
    svc = _service()
    body = {
        "properties": {"title": "CardScanner Inventory"},
        "sheets": [{"properties": {"title": "Inventory"}}],
    }
    res = svc.spreadsheets().create(body=body).execute()
    sid = res["spreadsheetId"]
    # Write header
    svc.spreadsheets().values().update(
        spreadsheetId=sid, range="Inventory!A1",
        valueInputOption="RAW", body={"values": [SHEET_HEADER]},
    ).execute()
    settings.google_sheet_id = sid  # in-process only; user should persist to .env
    return sid


def append_card(card: models.Card) -> bool:
    try:
        svc = _service()
        sid = ensure_sheet()
    except Exception:
        return False
    row = [
        card.year, card.set_brand, card.player, card.card_no, card.parallel,
        card.condition,
        card.est_value_raw or "", card.comp_median or "",
        card.comp_low or "", card.comp_high or "", card.comp_url or "",
        card.status, card.channel or "",
        card.ebay_status, card.ebay_listing_id or "",
        "YES" if card.is_hit_watchlist else "",
        card.notes or "",
        card.created_at.isoformat(),
    ]
    try:
        svc.spreadsheets().values().append(
            spreadsheetId=sid, range="Inventory!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()
        return True
    except HttpError:
        return False


def is_connected() -> bool:
    return Path(settings.google_token_path).exists()
