"""SQLModel data layer for CardScanner.

Designed so that the eBay listing flow is a first-class citizen, not a
retrofit: every Card has nullable listing fields and a child Listing rows
for full history.
"""
from __future__ import annotations

from datetime import datetime, date
from typing import Optional
from sqlmodel import SQLModel, Field


# ---------------------------------------------------------------------------
# Card — one row per individually-tracked baseball card.
# ---------------------------------------------------------------------------
class Card(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)

    # Identification (what Claude vision returns)
    year: Optional[int] = None
    set_brand: Optional[str] = None  # "Topps Finest"
    player: Optional[str] = None
    card_no: Optional[str] = None  # often alphanumeric e.g. "BCP-12"
    parallel: Optional[str] = "Base"
    sport: str = "Baseball"
    team: Optional[str] = None

    # Condition
    condition: Optional[str] = None  # NM / EX / VG / GD / PR  or "Graded PSA 9"
    is_graded: bool = False
    grade: Optional[str] = None  # "PSA 9" etc.
    is_autograph: bool = False
    is_relic: bool = False

    # Valuation
    est_value_raw: Optional[float] = None
    est_value_graded: Optional[float] = None
    comp_median: Optional[float] = None
    comp_low: Optional[float] = None
    comp_high: Optional[float] = None
    comp_count: int = 0
    comp_url: Optional[str] = None
    comp_fetched_at: Optional[datetime] = None

    # Storage / status
    storage: Optional[str] = None  # legacy free-text; superseded by storage_location_id (m10)
    storage_location_id: Optional[int] = Field(default=None, index=True)  # FK -> storagelocation.id
    storage_position: Optional[str] = None  # free-text per-card position (e.g. "p5/s3", "row 14")
    status: str = "Researching"  # Researching / Ready / Listed / Sold / Bulk
    channel: Optional[str] = None  # eBay / Facebook / LCS / etc.
    notes: Optional[str] = None

    # Hit-watchlist match
    is_hit_watchlist: bool = False
    hit_reason: Optional[str] = None

    # Action queue flags
    needs_photo_verification: bool = False
    consider_grading: bool = False
    review_flagged: bool = False  # Claude was unsure

    # A3: vision intelligence fields (Round 3)
    is_rookie: bool = False
    is_serial_numbered: bool = False
    serial_print_run: Optional[int] = None
    photo_quality: Optional[str] = None
    low_confidence_fields: Optional[str] = None  # JSON-encoded list
    condition_signals: Optional[str] = None      # JSON-encoded dict

    # B2: comp intelligence fields (Round 3)
    comp_confidence: Optional[str] = None         # high / medium / low
    comp_median_weighted: Optional[float] = None
    comp_suspicious_bulk: bool = False
    comp_suspicious_reason: Optional[str] = None

    # B6: Grading submission + bulk-lot fields (Round 4)
    grading_submission_id: Optional[str] = None  # uuid linking cards in a submission
    user_overrides: Optional[str] = None          # JSON-encoded list of user-edited fields
    lot_id: Optional[int] = None                  # FK to BulkLot (not enforced in SQLite)

    # Sale outcome
    sold_price: Optional[float] = None
    sold_date: Optional[date] = None
    fee_pct: Optional[float] = None  # 0.13 default for eBay

    # A3: Sales tracking fields (Round 4)
    sold_at: Optional[datetime] = None          # exact sale datetime (vs sold_date which is date-only)
    sale_channel: Optional[str] = None          # "eBay" / "FB" / "LCS" / etc.
    acquisition_cost: Optional[float] = None    # what Connor paid for this card

    # Image filenames (under data/uploads/)
    front_image: Optional[str] = None
    back_image: Optional[str] = None
    front_hash: Optional[str] = Field(default=None, index=True)
    back_hash: Optional[str] = Field(default=None, index=True)
    drive_front_id: Optional[str] = None
    drive_back_id: Optional[str] = None

    # Source scan job (for traceability)
    scan_job_id: Optional[int] = Field(default=None, foreign_key="scanjob.id")

    # eBay listing summary (denormalised for fast reads on the dashboard)
    ebay_status: str = "not_listed"  # not_listed / drafted / active / sold / ended
    ebay_listing_id: Optional[str] = None
    ebay_offer_id: Optional[str] = None

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)

    # ------------------------------------------------------------------
    def display_title(self) -> str:
        bits = []
        if self.year:
            bits.append(str(self.year))
        if self.set_brand:
            bits.append(self.set_brand)
        if self.player:
            bits.append(self.player)
        if self.card_no:
            bits.append(f"#{self.card_no}")
        if self.parallel and self.parallel.lower() != "base":
            bits.append(self.parallel)
        return " ".join(bits) if bits else f"Untitled Card #{self.id}"

    def era(self) -> str:
        y = self.year or 0
        if y == 0:
            return "Unknown"
        if y < 1986:
            return "Vintage (pre-1986)"
        if y < 1992:
            return "Junk Wax (1986-1991)"
        if y < 2000:
            return "Transitional (1992-1999)"
        if y < 2015:
            return "Modern (2000-2014)"
        return "Ultra-Modern (2015+)"


# ---------------------------------------------------------------------------
# ScanJob — represents one bulk-upload batch.
# ---------------------------------------------------------------------------
class ScanJob(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    label: Optional[str] = None
    status: str = "queued"  # queued / processing / done / error / abandoned
    total: int = 0
    processed: int = 0
    failed: int = 0
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
    # A4: JSON list of source image paths/IDs for batch-resume
    source_manifest: Optional[str] = None
    # m11: when the user kicks off sync, they can declare "this whole batch
    # came from <location>" — every Card the job creates inherits these.
    storage_location_id: Optional[int] = None
    storage_position: Optional[str] = None


# ---------------------------------------------------------------------------
# JobFailure — one row per failed file in a scan job. Surfaces the actual
# error message in the dashboard so the user knows WHY something failed.
# ---------------------------------------------------------------------------
class JobFailure(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    scan_job_id: Optional[int] = Field(default=None, foreign_key="scanjob.id", index=True)
    file_name: str
    drive_id: Optional[str] = None  # Drive file ID, when applicable
    error: str                       # short, human-readable
    error_class: Optional[str] = None
    occurred_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# AchievementUnlock — log of unlocked achievements (one row per unlock).
# ---------------------------------------------------------------------------
class AchievementUnlock(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    code: str = Field(index=True)  # matches catalog code
    unlocked_at: datetime = Field(default_factory=datetime.utcnow)
    seen: bool = False  # has the user seen the celebration?
    payload: Optional[str] = None  # JSON metadata e.g. card id


# ---------------------------------------------------------------------------
# DailyActivity — used to compute daily streak.
# ---------------------------------------------------------------------------
class DailyActivity(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    day: date = Field(index=True, unique=True)
    cards_added: int = 0


# ---------------------------------------------------------------------------
# Listing — full history of eBay listings per card.
# ---------------------------------------------------------------------------
class Listing(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    card_id: int = Field(foreign_key="card.id", index=True)

    marketplace: str = "ebay"
    environment: str = "sandbox"  # sandbox or production

    # eBay identifiers (filled in as the lifecycle progresses)
    sku: Optional[str] = None
    offer_id: Optional[str] = None
    listing_id: Optional[str] = None

    # Listing template / what we sent
    title: Optional[str] = None
    format: str = "AUCTION"  # AUCTION or FIXED_PRICE
    price: Optional[float] = None  # BIN price OR auction start price
    duration: str = "DAYS_7"
    category_id: Optional[str] = None
    description: Optional[str] = None

    status: str = "draft"  # draft / active / sold / ended / error
    error_message: Optional[str] = None

    # Sale outcome
    sold_price: Optional[float] = None
    sold_at: Optional[datetime] = None
    fees: Optional[float] = None

    # A3: Sales sync tracking fields (Round 4)
    last_synced_at: Optional[datetime] = None   # when sales_sync last touched this row
    sync_error: Optional[str] = None            # last sync error message if any

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# BulkLot — a proposed or created bulk-lot listing.
# ---------------------------------------------------------------------------
class BulkLot(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    label: str
    listing_title: Optional[str] = None
    price: Optional[float] = None
    status: str = "draft"            # draft / listed / sold
    ebay_listing_id: Optional[str] = None
    sold_price: Optional[float] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


# ---------------------------------------------------------------------------
# Hit Watchlist — pre-populated from Connor's existing tab.
# ---------------------------------------------------------------------------
class HitWatchlistEntry(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    year_min: Optional[int] = None
    year_max: Optional[int] = None
    set_pattern: Optional[str] = None  # case-insensitive substring
    player_pattern: Optional[str] = None
    card_no: Optional[str] = None
    typical_value: Optional[float] = None
    reason: Optional[str] = None


# ---------------------------------------------------------------------------
# StorageLocation — a named place a card sits (binder, box, toploader case…).
# ---------------------------------------------------------------------------
# `name` is unique case-insensitively at the DB layer (`COLLATE NOCASE` is
# applied in the m10 migration). The display value preserves whatever case
# the user originally typed; lookups normalize via case-insensitive compare.
KNOWN_STORAGE_KINDS = (
    "binder", "box", "toploader_case", "sleeve_page", "safe", "other"
)


class StorageLocation(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True)        # e.g. "Binder A", "Long Box 2"
    kind: str = "other"                  # display hint; see KNOWN_STORAGE_KINDS
    notes: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


def normalize_location_name(raw: str) -> str:
    """Trim and collapse internal whitespace so 'Box  A' == 'Box A'.

    Case is preserved — case-insensitive collation handles 'box a' == 'Box A'.
    """
    return " ".join((raw or "").split())
