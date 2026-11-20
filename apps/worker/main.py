"""Dramatiq worker entry point.

Run: ``dramatiq apps.worker.main --queues default default_high default_low``

One generic actor dispatches by ``kind`` to plain handler functions registered in
``HANDLERS``. Handlers stay thin: parse payload, call a service.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import dramatiq
import structlog

from clipforge.infra.config import get_settings
from clipforge.infra.logging import bind_context, configure_logging
from clipforge.infra.registry import DISPATCH_ACTOR, build_worker_broker

settings = get_settings()
configure_logging(settings.log_level, settings.log_json)
broker = build_worker_broker(settings)
log = structlog.get_logger("worker")

Handler = Callable[[dict[str, Any]], None]
HANDLERS: dict[str, Handler] = {}


def handler(kind: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        HANDLERS[kind] = fn
        return fn

    return deco


@handler("noop")
def _noop(payload: dict[str, Any]) -> None:
    log.info("noop_job", payload_keys=sorted(payload))


@dramatiq.actor(actor_name=DISPATCH_ACTOR, max_retries=3, min_backoff=2_000, max_backoff=60_000)
def dispatch(kind: str, job_id: str, payload: dict[str, Any]) -> None:
    fn = HANDLERS.get(kind)
    with bind_context(
        job_id=job_id,
        kind=kind,
        video_id=payload.get("video_id", ""),
        workspace_id=payload.get("workspace_id", ""),
    ):
        if fn is None:
            log.error("unknown_job_kind")
            return  # do not retry unknown kinds
        fn(payload)
