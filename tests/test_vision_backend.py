"""Backend-selection + CLI-subprocess tests for the vision service."""
import asyncio
import json
import os
import shutil
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from PIL import Image

from app.services import claude_vision
from app.services.claude_vision import (
    CardIdentification, _backend, active_backend, cli_available, identify_card_async,
)


# ---------------------------------------------------------------------------
# _backend selector
# ---------------------------------------------------------------------------
def test_backend_explicit_env_wins(monkeypatch):
    monkeypatch.setenv("CLAUDE_BACKEND", "http")
    assert _backend(None) == "http"
    monkeypatch.setenv("CLAUDE_BACKEND", "cli")
    assert _backend(None) == "cli"


def test_backend_per_request_key_forces_http(monkeypatch):
    monkeypatch.setenv("CLAUDE_BACKEND", "")
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/claude")
    assert _backend("sk-test-key") == "http"


def test_backend_prefers_cli_when_available(monkeypatch):
    monkeypatch.setenv("CLAUDE_BACKEND", "")
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/claude")
    assert _backend(None) == "cli"


def test_backend_falls_back_to_http_when_no_cli(monkeypatch):
    monkeypatch.setenv("CLAUDE_BACKEND", "")
    monkeypatch.setattr(shutil, "which", lambda _: None)
    assert _backend(None) == "http"


def test_active_backend_no_side_effects(monkeypatch):
    monkeypatch.setenv("CLAUDE_BACKEND", "http")
    assert active_backend() == "http"


# ---------------------------------------------------------------------------
# CLI subprocess invocation (mocked)
# ---------------------------------------------------------------------------
def _png(path: Path) -> Path:
    Image.new("RGB", (50, 70), "blue").save(path)
    return path


def _make_subprocess_mock(stdout_bytes: bytes, returncode: int = 0):
    """Return a mock that subs in for asyncio.create_subprocess_exec."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout_bytes, b""))
    proc.kill = MagicMock()
    return AsyncMock(return_value=proc)


def test_cli_call_parses_envelope(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_BACKEND", "cli")
    payload = {
        "year": 2018, "set_brand": "Bowman Chrome", "player": "Acuna",
        "card_no": "BCP-25", "parallel": "Refractor", "condition": "NM",
        "confidence": 0.9, "field_confidence": {"parallel": 0.9, "card_no": 0.9},
    }
    cli_envelope = json.dumps({"result": json.dumps(payload)}).encode()
    fake_spawn = _make_subprocess_mock(cli_envelope)

    front = _png(tmp_path / "front.png")

    with patch("asyncio.create_subprocess_exec", new=fake_spawn):
        cid = asyncio.run(identify_card_async(str(front)))

    assert cid.player == "Acuna"
    assert cid.year == 2018
    assert cid.parallel == "Refractor"


def test_cli_call_handles_raw_text_output(tmp_path, monkeypatch):
    """Older claude CLI versions return plain text instead of {result: ...}."""
    monkeypatch.setenv("CLAUDE_BACKEND", "cli")
    payload = {
        "year": 1989, "set_brand": "Upper Deck", "player": "Griffey",
        "card_no": "1", "parallel": "Base", "condition": "NM",
        "confidence": 0.95, "field_confidence": {"parallel": 1.0, "card_no": 1.0},
    }
    raw_text = json.dumps(payload).encode()
    fake_spawn = _make_subprocess_mock(raw_text)

    front = _png(tmp_path / "front.png")
    with patch("asyncio.create_subprocess_exec", new=fake_spawn):
        cid = asyncio.run(identify_card_async(str(front)))

    assert cid.player == "Griffey"
    assert cid.year == 1989


def test_cli_nonzero_exit_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_BACKEND", "cli")
    fake_proc = MagicMock()
    fake_proc.returncode = 2
    fake_proc.communicate = AsyncMock(return_value=(b"", b"auth required"))
    fake_proc.kill = MagicMock()
    fake_spawn = AsyncMock(return_value=fake_proc)

    front = _png(tmp_path / "front.png")
    with patch("asyncio.create_subprocess_exec", new=fake_spawn):
        with pytest.raises(RuntimeError, match="claude CLI exited 2"):
            asyncio.run(identify_card_async(str(front)))


def test_http_backend_still_works_when_explicitly_selected(tmp_path, monkeypatch):
    """Setting CLAUDE_BACKEND=http takes the legacy code path."""
    monkeypatch.setenv("CLAUDE_BACKEND", "http")
    monkeypatch.setattr(claude_vision.settings, "anthropic_api_key", "sk-test")

    payload = {
        "year": 2024, "set_brand": "Topps", "player": "Test", "card_no": "1",
        "parallel": "Base", "condition": "NM", "confidence": 0.9,
        "field_confidence": {"parallel": 0.9, "card_no": 0.9},
    }
    fake_resp = MagicMock()
    fake_resp.raise_for_status = MagicMock()
    fake_resp.json.return_value = {
        "content": [{"type": "text", "text": json.dumps(payload)}],
    }

    front = _png(tmp_path / "front.png")
    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=fake_resp)):
        cid = asyncio.run(identify_card_async(str(front)))

    assert cid.player == "Test"


def test_cli_available_is_boolean(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/claude")
    assert cli_available() is True
    monkeypatch.setattr(shutil, "which", lambda _: None)
    assert cli_available() is False
