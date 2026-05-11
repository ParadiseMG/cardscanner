"""Retry helper — 5xx retried, 4xx not."""
import asyncio
from unittest.mock import MagicMock

import httpx
import pytest

from app.utils.retry import with_backoff, is_retryable


def _http_err(status: int) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://x")
    resp = httpx.Response(status, request=req)
    return httpx.HTTPStatusError("err", request=req, response=resp)


def test_is_retryable_classifies_correctly():
    assert is_retryable(httpx.TimeoutException("t"))
    assert is_retryable(_http_err(500))
    assert is_retryable(_http_err(503))
    assert is_retryable(_http_err(429))
    assert not is_retryable(_http_err(400))
    assert not is_retryable(_http_err(401))
    assert not is_retryable(ValueError("other"))


@pytest.mark.asyncio
async def test_retries_then_succeeds(monkeypatch):
    # Make backoff sleeps instant
    import app.utils.retry as r
    async def fast_sleep(_): return None
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    n = {"calls": 0}
    async def flaky():
        n["calls"] += 1
        if n["calls"] < 3:
            raise _http_err(503)
        return "ok"

    result = await with_backoff(flaky, attempts=3, base_delay=0.0)
    assert result == "ok"
    assert n["calls"] == 3


@pytest.mark.asyncio
async def test_does_not_retry_4xx(monkeypatch):
    async def fast_sleep(_): return None
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)

    n = {"calls": 0}
    async def hard_fail():
        n["calls"] += 1
        raise _http_err(401)

    with pytest.raises(httpx.HTTPStatusError):
        await with_backoff(hard_fail, attempts=3, base_delay=0.0)
    assert n["calls"] == 1
