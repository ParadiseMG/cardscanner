"""B9: Tests for grading submission queue lifecycle."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlmodel import Session, select

from app import models
from app.db import get_engine, session_scope
from app.main import app

client = TestClient(app)


def _seed_card(player: str = "Test Player", comp_median: float = 100.0) -> int:
    """Insert a card and return its id."""
    with session_scope() as s:
        c = models.Card(player=player, year=2020, set_brand="Topps",
                        comp_median=comp_median)
        s.add(c)
        s.flush()
        cid = c.id
    return cid


class TestBuildSubmissionEndpoint:
    def test_returns_csv_content_type(self):
        cid = _seed_card()
        r = client.post("/api/grading/build-submission",
                        json={"card_ids": [cid], "service": "PSA", "service_level": "Value"})
        assert r.status_code == 200
        assert "text/csv" in r.headers["content-type"]

    def test_content_disposition_attachment(self):
        cid = _seed_card()
        r = client.post("/api/grading/build-submission",
                        json={"card_ids": [cid], "service": "PSA", "service_level": "Value"})
        assert "attachment" in r.headers["content-disposition"]
        assert "psa_submission" in r.headers["content-disposition"]

    def test_psa_csv_has_correct_header(self):
        cid = _seed_card()
        r = client.post("/api/grading/build-submission",
                        json={"card_ids": [cid], "service": "PSA", "service_level": "Value"})
        first_line = r.text.splitlines()[0]
        assert "Item" in first_line
        assert "Player" in first_line
        assert "Service Level" in first_line

    def test_sgc_csv_has_declared_value_column(self):
        cid = _seed_card()
        r = client.post("/api/grading/build-submission",
                        json={"card_ids": [cid], "service": "SGC", "service_level": "Standard"})
        assert r.status_code == 200
        first_line = r.text.splitlines()[0]
        assert "Declared Value" in first_line

    def test_missing_card_returns_404(self):
        r = client.post("/api/grading/build-submission",
                        json={"card_ids": [99999], "service": "PSA", "service_level": "Value"})
        assert r.status_code == 404

    def test_multiple_cards_in_csv(self):
        ids = [_seed_card(f"Player {i}") for i in range(3)]
        r = client.post("/api/grading/build-submission",
                        json={"card_ids": ids, "service": "PSA", "service_level": "Value"})
        # header + 3 data rows
        assert len(r.text.strip().splitlines()) == 4


class TestQueueEndpoint:
    def test_queue_sets_status_pending_grading(self):
        cid = _seed_card()
        r = client.post("/api/grading/queue",
                        json={"card_ids": [cid], "service": "PSA", "service_level": "Value"})
        assert r.status_code == 200
        with Session(get_engine()) as s:
            card = s.get(models.Card, cid)
        assert card.status == "Pending Grading"

    def test_queue_assigns_submission_id(self):
        cid = _seed_card()
        r = client.post("/api/grading/queue",
                        json={"card_ids": [cid], "service": "PSA", "service_level": "Value"})
        assert r.status_code == 200
        with Session(get_engine()) as s:
            card = s.get(models.Card, cid)
        assert card.grading_submission_id is not None
        assert len(card.grading_submission_id) == 36  # UUID

    def test_queue_returns_submission_id(self):
        cid = _seed_card()
        r = client.post("/api/grading/queue",
                        json={"card_ids": [cid], "service": "PSA", "service_level": "Value"})
        body = r.json()
        assert "submission_id" in body
        assert body["queued"] == 1

    def test_queue_appends_note(self):
        cid = _seed_card()
        client.post("/api/grading/queue",
                    json={"card_ids": [cid], "service": "PSA", "service_level": "Value"})
        with Session(get_engine()) as s:
            card = s.get(models.Card, cid)
        assert "Sent for PSA Value grading" in (card.notes or "")

    def test_queue_estimated_cost(self):
        ids = [_seed_card(f"P{i}") for i in range(3)]
        r = client.post("/api/grading/queue",
                        json={"card_ids": ids, "service": "PSA", "service_level": "Value"})
        body = r.json()
        # PSA Value = $25/card, 3 cards = $75
        assert body["estimated_cost"] == pytest.approx(75.0)

    def test_queue_groups_all_cards_same_submission_id(self):
        ids = [_seed_card(f"Player {i}") for i in range(3)]
        r = client.post("/api/grading/queue",
                        json={"card_ids": ids, "service": "PSA", "service_level": "Value"})
        sid = r.json()["submission_id"]
        with Session(get_engine()) as s:
            for cid in ids:
                card = s.get(models.Card, cid)
                assert card.grading_submission_id == sid


class TestGetQueuesEndpoint:
    def test_returns_queues_list(self):
        cid = _seed_card()
        client.post("/api/grading/queue",
                    json={"card_ids": [cid], "service": "PSA", "service_level": "Value"})
        r = client.get("/api/grading/queues")
        assert r.status_code == 200
        body = r.json()
        assert "submissions" in body
        assert len(body["submissions"]) >= 1

    def test_queue_group_has_correct_card_count(self):
        ids = [_seed_card(f"P{i}") for i in range(4)]
        r = client.post("/api/grading/queue",
                        json={"card_ids": ids, "service": "PSA", "service_level": "Value"})
        sid = r.json()["submission_id"]
        queues = client.get("/api/grading/queues").json()["submissions"]
        group = next((q for q in queues if q["submission_id"] == sid), None)
        assert group is not None
        assert group["card_count"] == 4

    def test_empty_queues_when_no_pending(self):
        r = client.get("/api/grading/queues")
        assert r.status_code == 200
        assert r.json()["submissions"] == []


class TestMarkBackEndpoint:
    def test_mark_back_sets_is_graded(self):
        cid = _seed_card()
        queue_r = client.post("/api/grading/queue",
                              json={"card_ids": [cid], "service": "PSA", "service_level": "Value"})
        sid = queue_r.json()["submission_id"]
        r = client.post(f"/api/grading/{sid}/mark-back",
                        json={"items": [{"card_id": cid, "grade": "9"}], "service": "PSA"})
        assert r.status_code == 200
        with Session(get_engine()) as s:
            card = s.get(models.Card, cid)
        assert card.is_graded is True

    def test_mark_back_sets_grade_string(self):
        cid = _seed_card()
        queue_r = client.post("/api/grading/queue",
                              json={"card_ids": [cid], "service": "PSA", "service_level": "Value"})
        sid = queue_r.json()["submission_id"]
        client.post(f"/api/grading/{sid}/mark-back",
                    json={"items": [{"card_id": cid, "grade": "9"}], "service": "PSA"})
        with Session(get_engine()) as s:
            card = s.get(models.Card, cid)
        assert card.grade == "PSA 9"

    def test_mark_back_sets_status_researching(self):
        cid = _seed_card()
        queue_r = client.post("/api/grading/queue",
                              json={"card_ids": [cid], "service": "PSA", "service_level": "Value"})
        sid = queue_r.json()["submission_id"]
        client.post(f"/api/grading/{sid}/mark-back",
                    json={"items": [{"card_id": cid, "grade": "9"}], "service": "PSA"})
        with Session(get_engine()) as s:
            card = s.get(models.Card, cid)
        assert card.status == "Researching"

    def test_mark_back_returns_updated_count(self):
        ids = [_seed_card(f"P{i}") for i in range(2)]
        queue_r = client.post("/api/grading/queue",
                              json={"card_ids": ids, "service": "PSA", "service_level": "Value"})
        sid = queue_r.json()["submission_id"]
        r = client.post(f"/api/grading/{sid}/mark-back",
                        json={"items": [
                            {"card_id": ids[0], "grade": "9"},
                            {"card_id": ids[1], "grade": "8"},
                        ], "service": "PSA"})
        assert r.json()["updated"] == 2

    def test_mark_back_ignores_wrong_submission_id(self):
        cid = _seed_card()
        # Queue it under one submission id
        client.post("/api/grading/queue",
                    json={"card_ids": [cid], "service": "PSA", "service_level": "Value"})
        # Mark back under a different (fake) submission id
        import uuid
        fake_sid = str(uuid.uuid4())
        r = client.post(f"/api/grading/{fake_sid}/mark-back",
                        json={"items": [{"card_id": cid, "grade": "9"}], "service": "PSA"})
        assert r.json()["updated"] == 0
