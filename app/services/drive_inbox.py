"""Google Drive as the photo inbox.

Folder layout (created on first run if missing):
    Baseball Cards/
        To Be Processed/
        Processed/
        Failed/

Workflow:
1. List image files in `To Be Processed`.
2. Pair files by base name when names end in `_front` / `_back`. Otherwise treat
   each file as a single-sided card.
3. Hash the bytes of the front image. If the hash already exists on a Card
   row, skip and move the file to Processed (it's a duplicate).
4. Run the standard pipeline (Claude vision → comp lookup → save).
5. On success: move file(s) to Processed and stamp Card.drive_front_id /
   drive_back_id with the moved-file IDs.
6. On failure: move file(s) to Failed and write an `errors.txt` annotation.

Drive scope is granted in the same OAuth flow as Sheets (see sheets_sync.py).
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload

from sqlmodel import Session, select

from app.config import settings, UPLOAD_DIR
from app import models
from app.db import session_scope
from app.utils import logger as _log

log = _log.get(__name__)


def _retry(call, *, attempts: int = 4, base_delay: float = 1.0):
    """Sync retry for googleapiclient calls (handles 429/5xx via HttpError)."""
    import time, random
    from googleapiclient.errors import HttpError
    last: Exception | None = None
    for n in range(1, attempts + 1):
        try:
            return call()
        except HttpError as e:
            sc = getattr(e, "status_code", None) or e.resp.status
            if sc not in (429, 500, 502, 503, 504) or n == attempts:
                raise
            last = e
        except Exception as e:
            if n == attempts:
                raise
            last = e
        delay = min(8.0, base_delay * (2 ** (n - 1)))
        delay *= 0.7 + 0.6 * random.random()
        log.warning("drive retry", extra={"attempt": n, "error": type(last).__name__})
        time.sleep(delay)
    raise last  # pragma: no cover


# ---------------------------------------------------------------------------
# Auth (re-uses Sheets OAuth credentials)
# ---------------------------------------------------------------------------
def _credentials() -> Credentials:
    p = Path(settings.google_token_path)
    if not p.exists():
        raise RuntimeError("Google not connected — run /auth/google/start first.")
    creds = Credentials.from_authorized_user_file(str(p))
    if not creds.valid and creds.refresh_token:
        from google.auth.transport.requests import Request
        creds.refresh(Request())
        p.write_text(creds.to_json())
    return creds


def _service():
    return build("drive", "v3", credentials=_credentials(), cache_discovery=False)


# ---------------------------------------------------------------------------
# Folder bootstrap
# ---------------------------------------------------------------------------
ROOT_NAME = "Baseball Cards"
SUBFOLDERS = ("To Be Processed", "Processed", "Failed")


@dataclass
class InboxFolders:
    root_id: str
    inbox_id: str
    processed_id: str
    failed_id: str


def _find_folder(svc, name: str, parent_id: Optional[str]) -> Optional[str]:
    q = (f"name = '{name.replace(chr(39), chr(92)+chr(39))}' "
         f"and mimeType = 'application/vnd.google-apps.folder' and trashed = false")
    if parent_id:
        q += f" and '{parent_id}' in parents"
    res = svc.files().list(q=q, fields="files(id, name)", spaces="drive").execute()
    files = res.get("files", [])
    return files[0]["id"] if files else None


def _create_folder(svc, name: str, parent_id: Optional[str]) -> str:
    body = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
    if parent_id:
        body["parents"] = [parent_id]
    return svc.files().create(body=body, fields="id").execute()["id"]


def ensure_folders() -> InboxFolders:
    svc = _service()
    root = _find_folder(svc, ROOT_NAME, None) or _create_folder(svc, ROOT_NAME, None)
    ids = {}
    for sub in SUBFOLDERS:
        ids[sub] = _find_folder(svc, sub, root) or _create_folder(svc, sub, root)
    return InboxFolders(
        root_id=root,
        inbox_id=ids["To Be Processed"],
        processed_id=ids["Processed"],
        failed_id=ids["Failed"],
    )


# ---------------------------------------------------------------------------
# File operations
# ---------------------------------------------------------------------------
IMAGE_MIMES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}
PAIR_RE = re.compile(r"^(?P<base>.+?)_(?P<side>front|back)\.(?P<ext>[A-Za-z0-9]+)$", re.I)


def list_inbox(folders: InboxFolders) -> list[dict]:
    svc = _service()
    res = _retry(lambda: svc.files().list(
        q=f"'{folders.inbox_id}' in parents and trashed = false",
        fields="files(id, name, mimeType, md5Checksum, size)",
        orderBy="createdTime",
        pageSize=200,
    ).execute())
    return [f for f in res.get("files", []) if f.get("mimeType") in IMAGE_MIMES]


def download_to(path: Path, file_id: str) -> Path:
    svc = _service()
    def _do():
        request = svc.files().get_media(fileId=file_id)
        buf = io.BytesIO()
        dl = MediaIoBaseDownload(buf, request)
        done = False
        while not done:
            _, done = dl.next_chunk()
        path.write_bytes(buf.getvalue())
        return path
    return _retry(_do)


def move_file(file_id: str, dest_folder_id: str) -> None:
    svc = _service()
    def _do():
        f = svc.files().get(fileId=file_id, fields="parents").execute()
        prev = ",".join(f.get("parents", []))
        svc.files().update(
            fileId=file_id, addParents=dest_folder_id, removeParents=prev,
            fields="id, parents",
        ).execute()
    _retry(_do)


def make_public(file_id: str) -> str:
    """Make a file publicly readable, return a direct-content URL eBay can fetch."""
    svc = _service()
    try:
        svc.permissions().create(
            fileId=file_id, body={"type": "anyone", "role": "reader"},
            fields="id",
        ).execute()
    except Exception:
        pass
    return f"https://drive.google.com/uc?id={file_id}&export=download"


# ---------------------------------------------------------------------------
# Pairing / hashing helpers
# ---------------------------------------------------------------------------
def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(64 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def already_have_hash(h: str) -> bool:
    with session_scope() as s:
        return s.exec(
            select(models.Card).where(
                (models.Card.front_hash == h) | (models.Card.back_hash == h)
            )
        ).first() is not None


def pair_files(files: list[dict]) -> list[tuple[Optional[dict], Optional[dict]]]:
    """Group files into (front, back) tuples. Unpaired files become (file, None)."""
    by_base: dict[str, dict[str, dict]] = {}
    singletons: list[dict] = []
    for f in files:
        m = PAIR_RE.match(f["name"])
        if m:
            base = m["base"].lower()
            side = m["side"].lower()
            by_base.setdefault(base, {})[side] = f
        else:
            singletons.append(f)
    out: list[tuple[Optional[dict], Optional[dict]]] = []
    for base, sides in by_base.items():
        out.append((sides.get("front"), sides.get("back")))
    for f in singletons:
        out.append((f, None))
    return out
