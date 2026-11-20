"""Structured JSON logging. Bind video_id / job_id / workspace_id with ``bind_context``."""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import Iterator, MutableMapping
from contextlib import contextmanager
from typing import Any

import structlog

_SECRET_KEYS = re.compile(r"(token|secret|password|api_key|authorization)", re.IGNORECASE)


def _redact(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    for k in list(event_dict):
        if _SECRET_KEYS.search(k):
            event_dict[k] = "***"
    return event_dict


def configure_logging(level: str = "INFO", json: bool = True) -> None:
    renderer: Any = structlog.processors.JSONRenderer() if json else structlog.dev.ConsoleRenderer()
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            _redact,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(logging.getLevelName(level.upper())),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


@contextmanager
def bind_context(**kv: Any) -> Iterator[None]:
    """Attach ids (video_id, job_id, workspace_id, ...) to every log line in this block."""
    tokens = structlog.contextvars.bind_contextvars(**{k: str(v) for k, v in kv.items()})
    try:
        yield
    finally:
        structlog.contextvars.reset_contextvars(**tokens)
