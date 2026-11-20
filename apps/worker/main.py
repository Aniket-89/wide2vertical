"""Dramatiq worker entry point.

Run: ``dramatiq apps.worker.main --queues default default_high default_low transcribe ...``

One generic actor dispatches by ``kind`` to plain handler functions registered in
``HANDLERS``. Handlers stay thin: parse payload, call a service.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import UUID

import dramatiq
import structlog

from clipforge.domain.values import WorkspaceId
from clipforge.infra.config import get_settings
from clipforge.infra.db.session import session_scope
from clipforge.infra.logging import bind_context, configure_logging
from clipforge.infra.registry import DISPATCH_ACTOR, build_worker_broker, get_container
from clipforge.ports.infra import Priority
from clipforge.ports.pipeline import PipelineError

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


def _ids(payload: dict[str, Any]) -> tuple[WorkspaceId, UUID]:
    return WorkspaceId(UUID(payload["workspace_id"])), UUID(payload["video_id"])


@handler("pipeline.fetch")
def _fetch(payload: dict[str, Any]) -> None:
    ws, video_id = _ids(payload)
    c = get_container()
    with session_scope(commit_on_error=True) as session:
        c.ingest_pipeline(session, ws).fetch_audio(video_id)
    c.queue.enqueue("pipeline.transcribe", payload, priority=Priority.NORMAL)


@handler("pipeline.transcribe")
def _transcribe(payload: dict[str, Any]) -> None:
    ws, video_id = _ids(payload)
    c = get_container()
    with session_scope(commit_on_error=True) as session:
        c.ingest_pipeline(session, ws).transcribe(video_id)
    c.queue.enqueue("pipeline.analyze", payload, priority=Priority.NORMAL)


@handler("pipeline.analyze")
def _analyze(payload: dict[str, Any]) -> None:
    ws, video_id = _ids(payload)
    with session_scope(commit_on_error=True) as session:
        res = (
            get_container()
            .analyze_pipeline(session, ws)
            .analyze(video_id, payload.get("scoring_profile"))
        )
    log.info("analyzed", candidates=len(res.candidates), chat=res.chat_status, signals=res.signals)


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
        try:
            fn(payload)
        except PipelineError as exc:
            # Expected failures (refused, unsupported, quota) are recorded in job_runs and
            # are not worth retrying; unexpected exceptions propagate to Dramatiq's retries.
            log.warning("job_failed", error=str(exc)[:500])
