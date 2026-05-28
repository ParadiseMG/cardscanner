"""Expanded /api/health returns a status object with per-backend checks."""
from fastapi.testclient import TestClient

from app.main import app


def test_health_returns_check_object():
    client = TestClient(app)
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert "checks" in body
    for k in ("db", "google", "ebay", "claude"):
        assert k in body["checks"]
    # DB has to be healthy in tests
    assert body["checks"]["db"]["ok"] is True
    assert "schema_version" in body["checks"]["db"]
