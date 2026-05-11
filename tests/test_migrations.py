"""Schema migration runner — idempotent, advances version."""
import sqlmodel as sm

from app import migrations


def _engine():
    e = sm.create_engine("sqlite:///:memory:")
    sm.SQLModel.metadata.create_all(e)
    return e


def test_run_advances_to_latest():
    e = _engine()
    v = migrations.run(e)
    assert v == migrations.MIGRATIONS[-1][0]


def test_run_is_idempotent():
    e = _engine()
    v1 = migrations.run(e)
    v2 = migrations.run(e)  # no-op second time
    assert v1 == v2


def test_indexes_exist_after_run():
    e = _engine()
    migrations.run(e)
    with e.begin() as conn:
        from sqlalchemy import text
        rows = conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'ix_card_%'"
        )).fetchall()
    names = {r[0] for r in rows}
    assert "ix_card_year_player" in names
