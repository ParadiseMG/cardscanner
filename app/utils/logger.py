"""Structured JSON logger.

Every pipeline step emits one line of JSON to stdout. Easy to grep, easy to
ingest into any log pipeline. Keeps the terminal-readability of plain logs
while being machine-parseable.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from contextlib import contextmanager
from typing import Any


class _JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": int(record.created * 1000),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for k, v in record.__dict__.items():
            if k in ("args", "msg", "levelname", "levelno", "pathname", "filename",
                     "module", "exc_info", "exc_text", "stack_info", "lineno",
                     "funcName", "created", "msecs", "relativeCreated", "thread",
                     "threadName", "processName", "process", "name"):
                continue
            try:
                json.dumps(v)
                payload[k] = v
            except TypeError:
                payload[k] = str(v)
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, separators=(",", ":"))


def configure(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # Quiet down noisy libs
    for name in ("uvicorn.access", "googleapiclient.discovery_cache"):
        logging.getLogger(name).setLevel(logging.WARNING)


_RESERVED = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "message",
}


def get(name: str) -> logging.Logger:
    return logging.getLogger(name)


@contextmanager
def step(logger: logging.Logger, name: str, **fields):
    """Context manager: log start/end of a step with duration in ms."""
    t0 = time.perf_counter()
    safe_fields = {k: v for k, v in fields.items() if k not in _RESERVED}
    try:
        yield
        ms = int((time.perf_counter() - t0) * 1000)
        logger.info(f"step {name} ok", extra={"step": name, "outcome": "ok",
                                              "duration_ms": ms, **safe_fields})
    except Exception as e:
        ms = int((time.perf_counter() - t0) * 1000)
        logger.exception(f"step {name} fail", extra={"step": name, "outcome": "fail",
                                                     "duration_ms": ms,
                                                     "error": str(e), **safe_fields})
        raise
