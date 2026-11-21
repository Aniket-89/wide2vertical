"""/api/v1/candidates/{id} review actions and /api/v1/clips (render, detail, download)."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from apps.api.deps import ContainerDep, PrincipalDep, SessionDep
from clipforge.domain.values import BlobKey
from clipforge.infra.db.render_store import SqlRenderStore
from clipforge.ports.infra import Priority
from clipforge.ports.pipeline import PipelineError
from clipforge.ports.render import ClipRecord, RenderRecord
from clipforge.services._common import SourceNotAllowed
from clipforge.services.render import export_description
from clipforge.services.review import ReviewInvalid, reused_content_warnings

router = APIRouter(tags=["review"])


class CandidatePatch(BaseModel):
    status: Literal["pending", "approved", "rejected"] | None = None
    start_s: float | None = Field(default=None, ge=0)
    end_s: float | None = Field(default=None, gt=0)
    caption_text: str | None = Field(default=None, max_length=2000)
    layout: str | None = None
    caption_style_id: UUID | None = None
    crop_override: dict[str, Any] | None = None
    clear_crop_override: bool = False
    commentary_text: str | None = Field(default=None, max_length=200)


class RenderOut(BaseModel):
    id: UUID
    kind: str
    status: str
    duration_s: float | None
    render_seconds: float | None
    download_url: str | None


class ClipOut(BaseModel):
    id: UUID
    candidate_id: UUID
    video_id: UUID
    start_s: float
    end_s: float
    layout: str
    caption_style_id: UUID | None
    caption_override: dict[str, Any] | None
    crop_override: dict[str, Any] | None
    commentary_text: str
    credit_text: str
    renders: list[RenderOut]
    render_status: dict[str, str | None]
    export_description: str
    warnings: list[str]


class ReviewOut(BaseModel):
    candidate_id: UUID
    status: str
    changes: list[str]
    clip: ClipOut | None
    final_render_queued: bool


class RenderIn(BaseModel):
    kind: Literal["preview", "final"] = "preview"
    force: bool = False


def _render_out(r: RenderRecord | None) -> RenderOut | None:
    if r is None:
        return None
    url = f"/api/v1/clips/{r.clip_id}/download?kind={r.kind}" if r.status == "succeeded" else None
    return RenderOut(
        id=r.id,
        kind=r.kind,
        status=r.status,
        duration_s=r.duration_s,
        render_seconds=r.render_seconds,
        download_url=url,
    )


def _clip_out(store: SqlRenderStore, clip: ClipRecord) -> ClipOut:
    cand = store.get_candidate(clip.candidate_id)
    renders = [
        o for k in ("preview", "final") if (o := _render_out(store.latest_render(clip.id, k)))
    ]
    title = cand.title_suggestions[0] if cand and cand.title_suggestions else ""
    return ClipOut(
        id=clip.id,
        candidate_id=clip.candidate_id,
        video_id=clip.video_id,
        start_s=clip.start_s,
        end_s=clip.end_s,
        layout=clip.layout,
        caption_style_id=clip.caption_style_id,
        caption_override=dict(clip.caption_override) if clip.caption_override else None,
        crop_override=dict(clip.crop_override) if clip.crop_override else None,
        commentary_text=clip.commentary_text,
        credit_text=clip.credit_text,
        renders=renders,
        render_status=store.render_statuses(clip.id),
        export_description=export_description(
            title, cand.hook_text if cand else "", clip.credit_text
        ),
        warnings=reused_content_warnings(clip),
    )


def _enqueue_render(
    container: Any, workspace_id: UUID, clip_id: UUID, kind: str, force: bool = False
) -> None:
    container.queue.enqueue(
        f"render.{kind}",
        {"workspace_id": str(workspace_id), "clip_id": str(clip_id), "force": force},
        priority=Priority.HIGH if kind == "final" else Priority.NORMAL,
    )


@router.patch("/candidates/{candidate_id}", response_model=ReviewOut)
def review_candidate(
    candidate_id: UUID,
    body: CandidatePatch,
    principal: PrincipalDep,
    session: SessionDep,
    container: ContainerDep,
) -> ReviewOut:
    """Approve / reject / trim / edit caption, layout, crop or intro. Approving (or editing an
    approved clip) queues the final render. Every change is recorded as a feedback event."""
    svc = container.review_service(session, principal.workspace_id, principal.user_id)
    try:
        out = svc.update(candidate_id, **body.model_dump(exclude_unset=True))
    except SourceNotAllowed as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, exc.reason) from exc
    except ReviewInvalid as exc:
        code = status.HTTP_404_NOT_FOUND if "not found" in str(exc) else 422
        raise HTTPException(code, str(exc)) from exc
    session.commit()  # the worker must see the clip before it picks up the render job
    if out.final_render_needed and out.clip is not None:
        _enqueue_render(container, principal.workspace_id, out.clip.id, "final")
    store = SqlRenderStore(session, principal.workspace_id)
    return ReviewOut(
        candidate_id=out.candidate.id,
        status=out.candidate.status,
        changes=out.changes,
        clip=_clip_out(store, out.clip) if out.clip else None,
        final_render_queued=out.final_render_needed and out.clip is not None,
    )


@router.post(
    "/candidates/{candidate_id}/preview",
    response_model=ClipOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def preview_candidate(
    candidate_id: UUID, principal: PrincipalDep, session: SessionDep, container: ContainerDep
) -> ClipOut:
    pipe = container.render_pipeline(session, principal.workspace_id, principal.user_id)
    try:
        clip = pipe.ensure_clip(candidate_id)
    except PipelineError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    session.commit()
    _enqueue_render(container, principal.workspace_id, clip.id, "preview")
    return _clip_out(SqlRenderStore(session, principal.workspace_id), clip)


@router.get("/clips/{clip_id}", response_model=ClipOut)
def get_clip(clip_id: UUID, principal: PrincipalDep, session: SessionDep) -> ClipOut:
    store = SqlRenderStore(session, principal.workspace_id)
    clip = store.get_clip(clip_id)
    if clip is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "clip not found")
    return _clip_out(store, clip)


@router.post(
    "/clips/{clip_id}/render", response_model=ClipOut, status_code=status.HTTP_202_ACCEPTED
)
def render_clip(
    clip_id: UUID,
    body: RenderIn,
    principal: PrincipalDep,
    session: SessionDep,
    container: ContainerDep,
) -> ClipOut:
    store = SqlRenderStore(session, principal.workspace_id)
    clip = store.get_clip(clip_id)
    if clip is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "clip not found")
    if body.kind == "final":
        cand = store.get_candidate(clip.candidate_id)
        if cand is None or cand.status != "approved":
            raise HTTPException(status.HTTP_409_CONFLICT, "final renders need an approved clip")
    _enqueue_render(container, principal.workspace_id, clip.id, body.kind, body.force)
    return _clip_out(store, clip)


@router.post("/clips/{clip_id}/publish", status_code=status.HTTP_501_NOT_IMPLEMENTED)
def publish_clip(clip_id: UUID, principal: PrincipalDep) -> dict[str, str]:
    """Auto-publishing arrives in M6. For now: download the final render and post it."""
    raise HTTPException(status.HTTP_501_NOT_IMPLEMENTED, "publishing arrives in milestone 6")


@router.get("/clips/{clip_id}/download")
def download_clip(
    clip_id: UUID,
    principal: PrincipalDep,
    session: SessionDep,
    container: ContainerDep,
    kind: Annotated[Literal["preview", "final"], Query()] = "final",
) -> FileResponse:
    store = SqlRenderStore(session, principal.workspace_id)
    clip = store.get_clip(clip_id)  # workspace-scoped: no cross-tenant downloads
    if clip is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "clip not found")
    r = store.latest_render(clip.id, kind)
    if r is None or not r.blob_key:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"no {kind} render yet")
    key = BlobKey(r.blob_key)
    if key.workspace_id != principal.workspace_id or not container.blobs.exists(key):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "render file missing")
    cand = store.get_candidate(clip.candidate_id)
    title = cand.title_suggestions[0] if cand and cand.title_suggestions else f"clip-{clip.id}"
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", title).strip("-")[:60] or "clip"
    with container.blobs.open_local(key) as path:  # local store: the path stays valid
        return FileResponse(path, media_type="video/mp4", filename=f"{safe}-{kind}.mp4")
