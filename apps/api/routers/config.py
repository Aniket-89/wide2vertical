"""/api/v1/scoring-profiles and /api/v1/caption-styles.

Both are versioned and append-only: POSTing an existing name creates the next version, so
candidates and renders made with an older version stay explainable.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from apps.api.deps import PrincipalDep, SessionDep
from clipforge.infra.db import models as m
from clipforge.infra.db.repositories import AuditRepo, CaptionStyleRepo, ScoringProfileRepo
from clipforge.rendering.captions import validate_style_spec
from clipforge.scoring.profile import validate_profile

router = APIRouter(tags=["config"])

NAME = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9 _.\-]+$")


class ScoringProfileIn(BaseModel):
    name: str = NAME
    weights: dict[str, Any]
    params: dict[str, Any] = Field(default_factory=dict)
    is_default: bool = False


class ScoringProfileOut(BaseModel):
    id: UUID
    name: str
    version: int
    weights: dict[str, Any]
    params: dict[str, Any]
    is_default: bool
    created_at: datetime


class CaptionStyleIn(BaseModel):
    name: str = NAME
    spec: dict[str, Any]


class CaptionStyleOut(BaseModel):
    id: UUID
    name: str
    version: int
    spec: dict[str, Any]
    created_at: datetime


def _next_version(session: Session, model: Any, workspace_id: UUID, name: str) -> int:
    current = session.scalar(
        select(func.max(model.version)).where(
            model.workspace_id == workspace_id, model.name == name
        )
    )
    return int(current or 0) + 1


def _profile_out(p: m.ScoringProfile) -> dict[str, Any]:
    return {k: getattr(p, k) for k in ScoringProfileOut.model_fields}


def _style_out(s: m.CaptionStyle) -> dict[str, Any]:
    return {k: getattr(s, k) for k in CaptionStyleOut.model_fields}


@router.get("/scoring-profiles", response_model=list[ScoringProfileOut])
def list_profiles(principal: PrincipalDep, session: SessionDep) -> list[dict[str, Any]]:
    rows = ScoringProfileRepo(session, principal.workspace_id).list(limit=200)
    return [_profile_out(p) for p in sorted(rows, key=lambda p: (p.name, -p.version))]


@router.post(
    "/scoring-profiles", response_model=ScoringProfileOut, status_code=status.HTTP_201_CREATED
)
def create_profile(
    body: ScoringProfileIn, principal: PrincipalDep, session: SessionDep
) -> dict[str, Any]:
    try:
        weights, params = validate_profile(body.weights, body.params)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    ws = principal.workspace_id
    if body.is_default:
        session.execute(
            update(m.ScoringProfile)
            .where(m.ScoringProfile.workspace_id == ws)
            .values(is_default=False)
        )
    p = ScoringProfileRepo(session, ws).add(
        m.ScoringProfile(
            workspace_id=ws,
            name=body.name,
            version=_next_version(session, m.ScoringProfile, ws, body.name),
            weights=weights,
            params=params,
            is_default=body.is_default,
        )
    )
    AuditRepo(session, ws).record(
        "scoring_profile.created", "scoring_profile", p.id, principal.user_id, version=p.version
    )
    return _profile_out(p)


@router.get("/caption-styles", response_model=list[CaptionStyleOut])
def list_styles(principal: PrincipalDep, session: SessionDep) -> list[dict[str, Any]]:
    rows = CaptionStyleRepo(session, principal.workspace_id).list(limit=200)
    return [_style_out(s) for s in sorted(rows, key=lambda s: (s.name, -s.version))]


@router.post("/caption-styles", response_model=CaptionStyleOut, status_code=status.HTTP_201_CREATED)
def create_style(
    body: CaptionStyleIn, principal: PrincipalDep, session: SessionDep
) -> dict[str, Any]:
    try:
        spec = validate_style_spec(body.spec)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    ws = principal.workspace_id
    s = CaptionStyleRepo(session, ws).add(
        m.CaptionStyle(
            workspace_id=ws,
            name=body.name,
            version=_next_version(session, m.CaptionStyle, ws, body.name),
            spec=spec,
        )
    )
    AuditRepo(session, ws).record(
        "caption_style.created", "caption_style", s.id, principal.user_id, version=s.version
    )
    return _style_out(s)
