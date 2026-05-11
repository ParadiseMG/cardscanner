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
        card.est_value_raw or "", card.comp_median_weighted or card.comp_median or "",
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


# ---------------------------------------------------------------------------
# A4: Sales sheet mirror
# ---------------------------------------------------------------------------

SALES_SHEET_HEADER = [
    "Sold At", "Player", "Year", "Set/Brand", "Card #", "Parallel",
    "Channel", "Sold Price", "Fees", "Net", "Acquisition Cost", "Profit",
    "eBay Offer ID", "Notes",
]


def _ensure_sales_sheet(svc, sid: str) -> None:
    """Create a 'Sales' tab in the spreadsheet if it doesn't exist."""
    meta = svc.spreadsheets().get(spreadsheetId=sid).execute()
    existing = [s["properties"]["title"] for s in meta.get("sheets", [])]
    if "Sales" in existing:
        return
    # Add the sheet
    svc.spreadsheets().batchUpdate(
        spreadsheetId=sid,
        body={"requests": [{"addSheet": {"properties": {"title": "Sales"}}}]},
    ).execute()
    # Write header row
    svc.spreadsheets().values().update(
        spreadsheetId=sid, range="Sales!A1",
        valueInputOption="RAW", body={"values": [SALES_SHEET_HEADER]},
    ).execute()


def append_sale(card: models.Card) -> bool:
    """Append a sold-card row to the 'Sales' tab of the Google Sheet mirror.

    Creates the 'Sales' tab if it doesn't exist.  Silently returns False if
    Google Sheets is not connected or the write fails.
    """
    try:
        svc = _service()
        sid = ensure_sheet()
    except Exception:
        return False

    try:
        _ensure_sales_sheet(svc, sid)
    except Exception:
        return False

    sold_at_str = card.sold_at.isoformat() if card.sold_at else ""
    gross = card.sold_price or 0.0
    fees = round(gross * (card.fee_pct or 0.13), 2)
    net = round(gross - fees, 2)
    acq = card.acquisition_cost or ""
    profit = round(net - float(acq), 2) if acq else ""

    row = [
        sold_at_str,
        card.player or "",
        card.year or "",
        card.set_brand or "",
        card.card_no or "",
        card.parallel or "",
        card.sale_channel or card.channel or "eBay",
        gross,
        fees,
        net,
        acq,
        profit,
        card.ebay_offer_id or "",
        card.notes or "",
    ]
    try:
        svc.spreadsheets().values().append(
            spreadsheetId=sid, range="Sales!A1",
            valueInputOption="USER_ENTERED",
            insertDataOption="INSERT_ROWS",
            body={"values": [row]},
        ).execute()
        return True
    except HttpError:
        return False
