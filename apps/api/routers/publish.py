"""/api/v1 publishing: targets, schedule a clip, list and cancel publications."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from apps.api.deps import ContainerDep, PrincipalDep, SessionDep
from clipforge.infra.db.publish_store import SqlPublishStore
from clipforge.ports.infra import Priority
from clipforge.ports.publish import PublicationRecord, PublishRefused

router = APIRouter(tags=["publish"])


class TargetOut(BaseModel):
    id: UUID
    platform: str
    account_label: str
    enabled: bool
    connected: bool


class PublishIn(BaseModel):
    target_id: UUID | None = None  # default: the first enabled YouTube target
    title: str | None = Field(default=None, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    privacy: Literal["private", "unlisted", "public"] = "private"
    scheduled_at: datetime | None = None  # None = now


class StatsOut(BaseModel):
    views: int | None
    likes: int | None
    comments: int | None
    at: datetime


class PublicationOut(BaseModel):
    id: UUID
    clip_id: UUID
    target_id: UUID
    platform: str
    account_label: str
    status: str
    title: str
    description: str
    privacy: str
    scheduled_at: datetime | None
    published_at: datetime | None
    external_id: str | None
    url: str | None
    error: str | None
    stats: StatsOut | None


def _url(platform: str, external_id: str | None) -> str | None:
    if not external_id:
        return None
    return f"https://youtu.be/{external_id}" if platform == "youtube" else None


def _outs(store: SqlPublishStore, pubs: list[PublicationRecord]) -> list[PublicationOut]:
    targets = {t.id: t for t in store.list_targets()}
    perf = store.latest_performance([p.id for p in pubs])
    out = []
    for p in pubs:
        t = targets.get(p.target_id)
        platform = t.platform if t else "unknown"
        s = perf.get(p.id)
        out.append(
            PublicationOut(
                id=p.id,
                clip_id=p.clip_id,
                target_id=p.target_id,
                platform=platform,
                account_label=t.account_label if t else "",
                status=p.status,
                title=p.title,
                description=p.description,
                privacy=p.privacy,
                scheduled_at=p.scheduled_at,
                published_at=p.published_at,
                external_id=p.external_id,
                url=_url(platform, p.external_id),
                error=p.error,
                stats=StatsOut(**s) if s else None,
            )
        )
    return out


@router.get("/publish-targets", response_model=list[TargetOut])
def list_targets(principal: PrincipalDep, session: SessionDep) -> list[TargetOut]:
    return [
        TargetOut(
            id=t.id,
            platform=t.platform,
            account_label=t.account_label,
            enabled=t.enabled,
            connected=bool(t.secret_ref),
        )
        for t in SqlPublishStore(session, principal.workspace_id).list_targets()
    ]


@router.post(
    "/clips/{clip_id}/publish",
    response_model=PublicationOut,
    status_code=status.HTTP_202_ACCEPTED,
)
def publish_clip(
    clip_id: UUID,
    body: PublishIn,
    principal: PrincipalDep,
    session: SessionDep,
    container: ContainerDep,
) -> PublicationOut:
    """Schedule (or publish now) an approved clip's final render. Always an explicit
    request; the same clip + target + render is never posted twice."""
    ws = principal.workspace_id
    store = SqlPublishStore(session, ws)
    target_id = body.target_id
    if target_id is None:
        t = store.default_target("youtube")
        if t is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "connect a YouTube channel first: clipforge publish connect-youtube",
            )
        target_id = t.id
    at = body.scheduled_at
    if at is not None and at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    svc = container.publish_service(session, ws, principal.user_id)
    try:
        pub = svc.schedule(
            clip_id,
            target_id,
            title=body.title,
            description=body.description,
            privacy=body.privacy,
            at=at,
        )
    except PublishRefused as exc:
        code = status.HTTP_404_NOT_FOUND if "not found" in str(exc) else status.HTTP_409_CONFLICT
        raise HTTPException(code, str(exc)) from exc
    claimed = svc.claim_if_due(pub)
    session.commit()  # the worker must see the row before it picks up the job
    if claimed:
        container.queue.enqueue(
            "publish.run",
            {"workspace_id": str(ws), "publication_id": str(pub.id)},
            priority=Priority.HIGH,
        )
    fresh = store.get_publication(pub.id)
    assert fresh is not None
    return _outs(store, [fresh])[0]


@router.get("/publications", response_model=list[PublicationOut])
def list_publications(
    principal: PrincipalDep,
    session: SessionDep,
    clip_id: Annotated[UUID | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
) -> list[PublicationOut]:
    store = SqlPublishStore(session, principal.workspace_id)
    return _outs(store, store.list_publications(clip_id, limit))


@router.post("/publications/{pub_id}/cancel", response_model=PublicationOut)
def cancel_publication(
    pub_id: UUID, principal: PrincipalDep, session: SessionDep, container: ContainerDep
) -> PublicationOut:
    store = SqlPublishStore(session, principal.workspace_id)
    pub = store.get_publication(pub_id)
    if pub is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "publication not found")
    svc = container.publish_service(session, principal.workspace_id, principal.user_id)
    if not svc.cancel(pub_id):
        raise HTTPException(status.HTTP_409_CONFLICT, f"can't cancel a {pub.status} post")
    fresh = store.get_publication(pub_id)
    assert fresh is not None
    return _outs(store, [fresh])[0]
