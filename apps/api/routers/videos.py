"""/api/v1/videos — submit a URL, list videos, per-stage progress."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, Response, status
from pydantic import BaseModel, Field

from apps.api.deps import ContainerDep, PrincipalDep, SessionDep
from clipforge.infra.db import models as m
from clipforge.infra.db.pipeline_store import SqlPipelineStore, stage_overview
from clipforge.infra.db.repositories import ScoringProfileRepo, VideoRepo
from clipforge.ports.infra import Priority
from clipforge.ports.pipeline import PipelineError, UnsupportedUrl
from clipforge.services.ingest import SourceNotAllowed

router = APIRouter(prefix="/videos", tags=["videos"])


class VideoIn(BaseModel):
    url: str = Field(min_length=8, max_length=2000)
    override: bool = False
    scoring_profile_id: UUID | None = None


class VideoOut(BaseModel):
    id: UUID
    url: str
    platform: str
    external_id: str
    title: str
    duration_s: float | None
    status: str
    override_used: bool
    source_id: UUID | None
    created_at: datetime


class StageOut(BaseModel):
    stage: str
    status: str
    attempts: int
    error: str | None
    started_at: str | None
    finished_at: str | None


class VideoDetail(VideoOut):
    stages: list[StageOut]


def _out(v: m.Video) -> dict[str, Any]:
    return {k: getattr(v, k) for k in VideoOut.model_fields}


@router.post("", response_model=VideoOut, status_code=status.HTTP_201_CREATED)
def create_video(
    body: VideoIn,
    principal: PrincipalDep,
    session: SessionDep,
    container: ContainerDep,
    response: Response,
) -> dict[str, Any]:
    """Resolve + allowlist check happen synchronously (so refusals are immediate);
    fetch and transcription are queued for the worker."""
    if body.scoring_profile_id is not None:  # workspace-scoped lookup: no cross-tenant FK
        prof = ScoringProfileRepo(session, principal.workspace_id).get(body.scoring_profile_id)
        if prof is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "scoring profile not found")
    pipeline = container.ingest_pipeline(session, principal.workspace_id, principal.user_id)
    try:
        res = pipeline.ingest(body.url, override=body.override)
    except SourceNotAllowed as exc:
        session.commit()  # keep the audit record of the refusal
        raise HTTPException(status.HTTP_403_FORBIDDEN, exc.reason) from exc
    except UnsupportedUrl as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    except PipelineError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc

    video = VideoRepo(session, principal.workspace_id).get(res.video.id)
    assert video is not None
    if body.scoring_profile_id:
        video.scoring_profile_id = body.scoring_profile_id
    session.commit()  # the worker must see the row before it picks up the job
    container.queue.enqueue(
        "pipeline.fetch",
        {"workspace_id": str(principal.workspace_id), "video_id": str(video.id)},
        priority=Priority.NORMAL,
    )
    if not res.created:
        response.status_code = status.HTTP_200_OK
    return _out(video)


@router.get("", response_model=list[VideoOut])
def list_videos(
    principal: PrincipalDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict[str, Any]]:
    return [
        _out(v) for v in VideoRepo(session, principal.workspace_id).list(limit=limit, offset=offset)
    ]


@router.get("/{video_id}", response_model=VideoDetail)
def get_video(video_id: UUID, principal: PrincipalDep, session: SessionDep) -> dict[str, Any]:
    v = VideoRepo(session, principal.workspace_id).get(video_id)
    if v is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "video not found")
    return {**_out(v), "stages": stage_overview(session, principal.workspace_id, video_id)}


class CandidateOut(BaseModel):
    id: UUID
    rank: int
    start_s: float
    end_s: float
    score: float
    components: dict[str, Any]
    reason: str
    status: str


@router.get("/{video_id}/candidates", response_model=list[CandidateOut])
def list_video_candidates(
    video_id: UUID,
    principal: PrincipalDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> list[dict[str, Any]]:
    """Ranked candidates (best first) with per-signal component scores."""
    if VideoRepo(session, principal.workspace_id).get(video_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "video not found")
    rows = SqlPipelineStore(session, principal.workspace_id).list_candidates(video_id, limit=limit)
    return [
        {
            "id": c.id,
            "rank": c.rank,
            "start_s": c.start_s,
            "end_s": c.end_s,
            "score": c.score,
            "components": c.components,
            "reason": c.reason,
            "status": c.status,
        }
        for c in rows
    ]
