"""Helpers shared by pipeline services: stage tracking and takedown re-checks."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from uuid import UUID

from clipforge.domain.events import StageCompleted, StageFailed
from clipforge.domain.values import JobStatus, SourceStatus, Stage
from clipforge.ports.infra import EventBus
from clipforge.ports.pipeline import PipelineError, PipelineStore, VideoRecord


class SourceNotAllowed(PipelineError):
    """The allowlist refused this URL / a takedown stopped processing. ``reason`` is
    user-facing."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def sha256_text(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


@contextmanager
def track_stage(
    store: PipelineStore, events: EventBus, video_id: UUID, stage: Stage, input_hash: str
) -> Iterator[list[bool]]:
    """Track a stage in job_runs. Yield a 1-item list; set [0]=True to mark it SKIPPED."""
    run_id = store.begin_stage(video_id, stage, input_hash)
    skipped = [False]
    try:
        yield skipped
    except Exception as exc:
        store.end_stage(run_id, JobStatus.FAILED, f"{type(exc).__name__}: {exc}"[:2000])
        store.set_video_status(video_id, f"{stage.value}_failed")
        events.publish(
            StageFailed(
                workspace_id=store.workspace_id,
                video_id=video_id,
                stage=stage,
                error=type(exc).__name__,
            )
        )
        raise
    store.end_stage(run_id, JobStatus.SKIPPED if skipped[0] else JobStatus.SUCCEEDED)
    store.set_video_status(video_id, f"{stage.value}_done")
    events.publish(StageCompleted(workspace_id=store.workspace_id, video_id=video_id, stage=stage))


def require_video(store: PipelineStore, video_id: UUID) -> VideoRecord:
    video = store.get_video(video_id)
    if video is None:
        raise PipelineError(f"video {video_id} not found in this workspace")
    return video


def require_not_retired(store: PipelineStore, video: VideoRecord, stage: Stage) -> None:
    """Takedowns apply to work already queued: re-check before every heavy stage."""
    src = store.source_for_video(video)
    if src is not None and src.status is SourceStatus.RETIRED:
        store.audit(
            "pipeline.blocked", "video", video.id, stage=stage.value, reason="source retired"
        )
        store.set_video_status(video.id, "source_retired")
        raise SourceNotAllowed("source is retired (takedown); processing stopped")
