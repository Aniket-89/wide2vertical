"""Idempotent seed: the single MVP workspace, its owner, and default config rows."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge.domain.values import Role, WorkspaceId
from clipforge.infra.config import Settings
from clipforge.infra.db import models as m

DEFAULT_WEIGHTS: dict[str, float] = {
    "chat_velocity": 0.30,
    "emote_burst": 0.10,
    "audio_energy": 0.20,
    "speech_intensity": 0.15,
    "llm_moment_score": 0.20,
    "keyword_boost": 0.05,
}
DEFAULT_PARAMS: dict[str, Any] = {
    "min_clip_s": 20,
    "max_clip_s": 60,
    "lead_in_s": 1.5,
    "tail_s": 1.0,
    "min_gap_s": 30,
    "max_clips_per_hour": 12,
    "judge_top_n_per_hour": 30,
    "smoothing_s": 3,
}
DEFAULT_CAPTION_STYLE: dict[str, Any] = {
    "font": "Montserrat ExtraBold",
    "size": 72,
    "primary": "#FFFFFF",
    "highlight": "#FFE600",
    "outline": "#000000",
    "outline_px": 6,
    "position": "lower_third",
    "safe_margin_bottom_px": 380,
    "words_per_line": 3,
}


def seed(session: Session, settings: Settings) -> WorkspaceId:
    ws_id = UUID(settings.default_workspace_id)
    user_id = UUID(settings.default_user_id)

    if session.get(m.Workspace, ws_id) is None:
        session.add(m.Workspace(id=ws_id, name="Personal", slug="personal"))
    if session.get(m.User, user_id) is None:
        session.add(m.User(id=user_id, email=settings.default_user_email, display_name="Owner"))
    session.flush()

    def exists(stmt: Any) -> bool:
        return session.scalars(stmt).first() is not None

    if not exists(
        select(m.Membership).where(
            m.Membership.workspace_id == ws_id, m.Membership.user_id == user_id
        )
    ):
        session.add(m.Membership(workspace_id=ws_id, user_id=user_id, role=Role.OWNER.value))

    if not exists(
        select(m.ScoringProfile).where(
            m.ScoringProfile.workspace_id == ws_id, m.ScoringProfile.name == "default"
        )
    ):
        session.add(
            m.ScoringProfile(
                workspace_id=ws_id,
                name="default",
                version=1,
                weights=DEFAULT_WEIGHTS,
                params=DEFAULT_PARAMS,
                is_default=True,
            )
        )

    if not exists(
        select(m.CaptionStyle).where(
            m.CaptionStyle.workspace_id == ws_id, m.CaptionStyle.name == "bold-yellow"
        )
    ):
        session.add(
            m.CaptionStyle(
                workspace_id=ws_id, name="bold-yellow", version=1, spec=DEFAULT_CAPTION_STYLE
            )
        )
    session.flush()
    return WorkspaceId(ws_id)
