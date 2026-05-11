"""Tiny home-grown migration runner.

Each migration is a callable that takes a sqlmodel engine and applies idempotent
DDL/DML. The current schema_version is stored in the `schema_version` table.
On startup we fast-forward to MIGRATIONS[-1].id.

Adding a migration: append `(id:int, name:str, fn:callable)` to MIGRATIONS.
Migrations should be safe to re-run (use `IF NOT EXISTS` guards).
"""
from __future__ import annotations

from typing import Callable, Iterable

from sqlalchemy import text
from sqlalchemy.engine import Engine

from app.utils import logger as _log

log = _log.get(__name__)


def _ensure_table(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS schema_version (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """))


def _applied_ids(engine: Engine) -> set[int]:
    with engine.begin() as conn:
        rows = conn.execute(text("SELECT id FROM schema_version")).fetchall()
        return {r[0] for r in rows}


def _record(engine: Engine, mid: int, name: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("INSERT OR REPLACE INTO schema_version (id, name) VALUES (:id, :name)"),
            {"id": mid, "name": name},
        )


# ---------------------------------------------------------------------------
# Migration list — append new ones here.
# ---------------------------------------------------------------------------
def m1_baseline(engine: Engine) -> None:
    """SQLModel.metadata.create_all already builds the baseline schema, so this
    is a no-op marker — its only purpose is to fix v=1 as the post-init state."""
    pass


def m2_add_card_indexes(engine: Engine) -> None:
    """Composite index on (year, player) for faster duplicate checks."""
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS ix_card_year_player ON card (year, player)"
        ))


def m3_perf_indexes(engine: Engine) -> None:
    """A5: Performance indexes for server-side query + facets."""
    stmts = [
        "CREATE INDEX IF NOT EXISTS ix_card_status ON card(status)",
        "CREATE INDEX IF NOT EXISTS ix_card_ebay_status ON card(ebay_status)",
        "CREATE INDEX IF NOT EXISTS ix_card_comp_median ON card(comp_median DESC)",
        "CREATE INDEX IF NOT EXISTS ix_card_year ON card(year)",
        "CREATE INDEX IF NOT EXISTS ix_card_is_hit ON card(is_hit_watchlist)",
        "CREATE INDEX IF NOT EXISTS ix_card_review ON card(review_flagged)",
        "CREATE INDEX IF NOT EXISTS ix_scanjob_status ON scanjob(status)",
    ]
    with engine.begin() as conn:
        for stmt in stmts:
            conn.execute(text(stmt))


def m4_add_scanjob_manifest(engine: Engine) -> None:
    """A4: Add source_manifest column to scanjob for batch resume."""
    with engine.begin() as conn:
        # SQLite allows adding columns; use IF NOT EXISTS guard via try/except
        try:
            conn.execute(text(
                "ALTER TABLE scanjob ADD COLUMN source_manifest TEXT"
            ))
        except Exception:
            # Column already exists (idempotent)
            pass


def m5_vision_fields(engine: Engine) -> None:
    """R3-A3: Add vision intelligence columns to card table."""
    columns = [
        ("is_rookie", "INTEGER NOT NULL DEFAULT 0"),
        ("is_serial_numbered", "INTEGER NOT NULL DEFAULT 0"),
        ("serial_print_run", "INTEGER"),
        ("photo_quality", "TEXT"),
        ("low_confidence_fields", "TEXT"),
        ("condition_signals", "TEXT"),
    ]
    with engine.begin() as conn:
        for col_name, col_def in columns:
            try:
                conn.execute(text(
                    f"ALTER TABLE card ADD COLUMN {col_name} {col_def}"
                ))
            except Exception:
                # Column already exists (idempotent)
                pass


def m6_comp_intelligence(engine: Engine) -> None:
    """R3-B2: Add comp intelligence columns to card table."""
    columns = [
        ("comp_confidence", "TEXT"),
        ("comp_median_weighted", "REAL"),
        ("comp_suspicious_bulk", "INTEGER NOT NULL DEFAULT 0"),
        ("comp_suspicious_reason", "TEXT"),
    ]
    with engine.begin() as conn:
        for col_name, col_def in columns:
            try:
                conn.execute(text(
                    f"ALTER TABLE card ADD COLUMN {col_name} {col_def}"
                ))
            except Exception:
                # Column already exists (idempotent)
                pass


def m7_sales_tracking(engine: Engine) -> None:
    """R4-A3: Add sales tracking columns to card and listing tables."""
    card_columns = [
        ("sold_at", "TEXT"),           # ISO datetime string
        ("sale_channel", "TEXT"),      # eBay / FB / LCS / etc.
        ("acquisition_cost", "REAL"),  # what Connor paid
    ]
    listing_columns = [
        ("last_synced_at", "TEXT"),    # ISO datetime of last sync
        ("sync_error", "TEXT"),        # last sync error message
    ]
    with engine.begin() as conn:
        for col_name, col_def in card_columns:
            try:
                conn.execute(text(
                    f"ALTER TABLE card ADD COLUMN {col_name} {col_def}"
                ))
            except Exception:
                # Column already exists (idempotent)
                pass
        for col_name, col_def in listing_columns:
            try:
                conn.execute(text(
                    f"ALTER TABLE listing ADD COLUMN {col_name} {col_def}"
                ))
            except Exception:
                pass


def m8_grading_bulk(engine: Engine) -> None:
    """R4-B6: Add grading submission + bulk-lot columns to card; create bulklot table."""
    card_columns = [
        ("grading_submission_id", "TEXT"),
        ("user_overrides", "TEXT"),
        ("lot_id", "INTEGER"),
    ]
    with engine.begin() as conn:
        for col_name, col_def in card_columns:
            try:
                conn.execute(text(
                    f"ALTER TABLE card ADD COLUMN {col_name} {col_def}"
                ))
            except Exception:
                # Column already exists (idempotent)
                pass

        # Create BulkLot table if not exists
        conn.execute(text("""
            CREATE TABLE IF NOT EXISTS bulklot (
                id INTEGER PRIMARY KEY,
                label TEXT NOT NULL,
                listing_title TEXT,
                price REAL,
                status TEXT NOT NULL DEFAULT 'draft',
                ebay_listing_id TEXT,
                sold_price REAL,
                created_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """))


MIGRATIONS: list[tuple[int, str, Callable[[Engine], None]]] = [
    (1, "baseline", m1_baseline),
    (2, "add card year/player index", m2_add_card_indexes),
    (3, "perf indexes", m3_perf_indexes),
    (4, "add scanjob source_manifest", m4_add_scanjob_manifest),
    (5, "vision intelligence fields", m5_vision_fields),
    (6, "comp intelligence fields", m6_comp_intelligence),
    (7, "sales tracking fields", m7_sales_tracking),
    (8, "grading and bulk-lot fields", m8_grading_bulk),
]


def current_version(engine: Engine) -> int:
    _ensure_table(engine)
    applied = _applied_ids(engine)
    return max(applied) if applied else 0


def run(engine: Engine) -> int:
    _ensure_table(engine)
    applied = _applied_ids(engine)
    for mid, name, fn in MIGRATIONS:
        if mid in applied:
            continue
        log.info("migration apply", extra={"migration_id": mid, "migration_name": name})
        fn(engine)
        _record(engine, mid, name)
    return current_version(engine)
