"""/api/v1/sources — allowlist management and takedown."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field

from apps.api.deps import PrincipalDep, SessionDep
from clipforge.domain.values import ClipPolicy, Platform
from clipforge.infra.db import models as m
from clipforge.infra.db.sources import SourceAdmin

router = APIRouter(prefix="/sources", tags=["sources"])


class SourceIn(BaseModel):
    platform: Platform
    channel_handle: str = Field(min_length=1, max_length=200)
    clip_policy: ClipPolicy = ClipPolicy.UNKNOWN
    policy_url: str | None = None
    required_credit_text: str = ""
    notes: str = ""
    channel_id: str | None = None


class SourcePatch(BaseModel):
    clip_policy: ClipPolicy | None = None
    policy_url: str | None = None
    required_credit_text: str | None = None
    notes: str | None = None
    channel_id: str | None = None


class SourceOut(BaseModel):
    id: UUID
    platform: str
    channel_handle: str
    channel_id: str | None
    clip_policy: str
    policy_url: str | None
    required_credit_text: str
    notes: str
    status: str
    retired_at: datetime | None
    created_at: datetime


class RetireIn(BaseModel):
    reason: str = ""


class RetireOut(BaseModel):
    clips_hidden: int


def _out(s: m.Source) -> dict[str, Any]:
    return {k: getattr(s, k) for k in SourceOut.model_fields}


@router.get("", response_model=list[SourceOut])
def list_sources(
    principal: PrincipalDep,
    session: SessionDep,
    limit: Annotated[int, Query(ge=1, le=200)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[dict[str, Any]]:
    return [_out(s) for s in SourceAdmin(session, principal.workspace_id).list(limit, offset)]


@router.post("", response_model=SourceOut, status_code=status.HTTP_201_CREATED)
def create_source(body: SourceIn, principal: PrincipalDep, session: SessionDep) -> dict[str, Any]:
    admin = SourceAdmin(session, principal.workspace_id, principal.user_id)
    try:
        s = admin.add(
            body.platform,
            body.channel_handle,
            body.clip_policy,
            **body.model_dump(exclude={"platform", "channel_handle", "clip_policy"}),
        )
    except ValueError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    return _out(s)


@router.patch("/{source_id}", response_model=SourceOut)
def update_source(
    source_id: UUID, body: SourcePatch, principal: PrincipalDep, session: SessionDep
) -> dict[str, Any]:
    admin = SourceAdmin(session, principal.workspace_id, principal.user_id)
    try:
        s = admin.update(source_id, **body.model_dump(exclude_unset=True))
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "source not found") from exc
    return _out(s)


@router.post("/{source_id}/retire", response_model=RetireOut)
def retire_source(
    source_id: UUID, body: RetireIn, principal: PrincipalDep, session: SessionDep
) -> RetireOut:
    """One-click takedown: block the source and hide all of its clips."""
    admin = SourceAdmin(session, principal.workspace_id, principal.user_id)
    try:
        n = admin.retire(source_id, body.reason)
    except LookupError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "source not found") from exc
    return RetireOut(clips_hidden=n)
