"""Retry helpers — async exponential backoff for HTTP-ish failures."""
from __future__ import annotations

import asyncio
import random
from typing import Awaitable, Callable, Iterable, TypeVar

import httpx

T = TypeVar("T")


def is_retryable(exc: Exception) -> bool:
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)):
        return True
    if isinstance(exc, httpx.HTTPStatusError):
        sc = exc.response.status_code
        return sc == 429 or 500 <= sc < 600
    return False


async def with_backoff(
    fn: Callable[[], Awaitable[T]], *,
    attempts: int = 3, base_delay: float = 1.0, max_delay: float = 8.0,
    on_retry: Callable[[int, Exception], None] | None = None,
) -> T:
    last_exc: Exception | None = None
    for n in range(1, attempts + 1):
        try:
            return await fn()
        except Exception as e:
            last_exc = e
            if n == attempts or not is_retryable(e):
                raise
            delay = min(max_delay, base_delay * (2 ** (n - 1)))
            delay = delay * (0.7 + 0.6 * random.random())  # jitter
            if on_retry:
                on_retry(n, e)
            await asyncio.sleep(delay)
    assert last_exc
    raise last_exc
