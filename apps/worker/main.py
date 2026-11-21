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
    # The profile chosen at submission (or re-analysis) travels with the payload by id, so a
    # newer version of the same profile doesn't silently change an in-flight analysis.
    raw = payload.get("scoring_profile_id")
    profile: str | UUID | None = UUID(raw) if raw else payload.get("scoring_profile")
    with session_scope(commit_on_error=True) as session:
        res = get_container().analyze_pipeline(session, ws).analyze(video_id, profile)
    log.info("analyzed", candidates=len(res.candidates), chat=res.chat_status, signals=res.signals)
    n = get_container().settings.preview_top_n
    if n > 0:  # auto-previews of the best candidates so review can start right away
        get_container().queue.enqueue(
            "render.preview_top", {**payload, "n": n}, priority=Priority.LOW
        )


@handler("render.preview_top")
def _preview_top(payload: dict[str, Any]) -> None:
    ws, video_id = _ids(payload)
    c = get_container()
    with session_scope(commit_on_error=True) as session:
        pipe = c.render_pipeline(session, ws)
        clip_ids = [
            str(pipe.ensure_clip(cand.id).id)
            for cand in pipe.store.list_candidates(video_id, limit=int(payload.get("n", 5)))
            if cand.status != "rejected"
        ]
    for clip_id in clip_ids:  # one job per clip: one failed render doesn't block the others
        c.queue.enqueue(
            "render.preview", {"workspace_id": str(ws), "clip_id": clip_id}, priority=Priority.LOW
        )


def _render(payload: dict[str, Any], kind: str) -> None:
    ws = WorkspaceId(UUID(payload["workspace_id"]))
    clip_id = UUID(payload["clip_id"])
    with session_scope(commit_on_error=True) as session:
        out = (
            get_container()
            .render_pipeline(session, ws)
            .render(clip_id, kind, force=bool(payload.get("force")))
        )
    log.info("render_done", kind=kind, cached=out.cached, layout=out.layout)
    if kind == "final":  # autopublish (off by default; own-channel sources only)
        c = get_container()
        with session_scope() as session:
            svc = c.publish_service(session, ws)
            pub = svc.maybe_autopublish(clip_id)
            claimed = pub is not None and svc.claim_if_due(pub)
        if pub is not None and claimed:
            c.queue.enqueue("publish.run", {"workspace_id": str(ws), "publication_id": str(pub.id)})


@handler("render.preview")
def _render_preview(payload: dict[str, Any]) -> None:
    _render(payload, "preview")


@handler("render.final")
def _render_final(payload: dict[str, Any]) -> None:
    _render(payload, "final")


@handler("publish.run")
def _publish_run(payload: dict[str, Any]) -> None:
    ws = WorkspaceId(UUID(payload["workspace_id"]))
    with session_scope(commit_on_error=True) as session:
        pub = get_container().publish_service(session, ws).run(UUID(payload["publication_id"]))
    log.info("publish_done", status=pub.status if pub else "missing")


@handler("publish.stats")
def _publish_stats(payload: dict[str, Any]) -> None:
    ws = WorkspaceId(UUID(payload["workspace_id"]))
    with session_scope(commit_on_error=True) as session:
        n = get_container().publish_service(session, ws).refresh_stats()
    log.info("stats_refreshed", rows=n)


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
