"""Set test DB path before any app modules import (env-driven config)."""
import os
import tempfile
from pathlib import Path

# Bind a per-process temp DB so the engine never touches the FUSE-backed real path.
_TMP = Path(tempfile.mkdtemp(prefix="cs_pytest_", dir="/tmp"))
os.environ["DB_PATH"] = str(_TMP / "cs.db")
os.environ["LOCAL_XLSX_PATH"] = ""
os.environ["GOOGLE_OAUTH_CLIENT_SECRETS"] = str(_TMP / "no_secret.json")
os.environ["EBAY_APP_ID"] = ""
os.environ["EBAY_CERT_ID"] = ""

import pytest
from sqlmodel import SQLModel
import sqlmodel
import app.config as cfg


@pytest.fixture(autouse=True)
def isolated_db(tmp_path):
    """Wipe the schema between tests so each test gets a clean slate."""
    import app.db as db
    # Recreate engine pointing to a fresh per-test DB file
    db_file = tmp_path / "cs.db"
    db._engine = sqlmodel.create_engine(f"sqlite:///{db_file}")
    db.init_db()
    yield
