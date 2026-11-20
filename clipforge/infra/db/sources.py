"""Source allowlist administration: add / update / list / retire (takedown)."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from clipforge.domain.values import (
    ClipPolicy,
    Platform,
    SourceStatus,
    WorkspaceId,
    normalise_handle,
)
from clipforge.infra.db import models as m
from clipforge.infra.db.repositories import AuditRepo, SourceRepo

EDITABLE = {
    "clip_policy",
    "policy_url",
    "required_credit_text",
    "notes",
    "channel_id",
    "crop_override",
}


class SourceAdmin:
    def __init__(
        self, session: Session, workspace_id: WorkspaceId, actor_user_id: UUID | None = None
    ) -> None:
        self.session = session
        self.workspace_id = workspace_id
        self.actor = actor_user_id
        self.repo = SourceRepo(session, workspace_id)
        self.audit = AuditRepo(session, workspace_id)

    def add(
        self,
        platform: Platform,
        channel_handle: str,
        clip_policy: ClipPolicy = ClipPolicy.UNKNOWN,
        **fields: Any,
    ) -> m.Source:
        handle = normalise_handle(channel_handle)
        if self.repo.find(platform.value, handle) is not None:
            raise ValueError(f"source {platform.value}/{handle} already exists")
        bad = set(fields) - EDITABLE
        if bad:
            raise ValueError(f"unknown fields: {sorted(bad)}")
        src = self.repo.add(
            m.Source(
                workspace_id=self.workspace_id,
                platform=platform.value,
                channel_handle=handle,
                clip_policy=clip_policy.value,
                status=SourceStatus.ACTIVE.value,
                **fields,
            )
        )
        # Backfill: videos ingested via override before this channel was allowlisted.
        res = self.session.execute(
            update(m.Video)
            .where(
                m.Video.workspace_id == self.workspace_id,
                m.Video.platform == platform.value,
                m.Video.source_id.is_(None),
                m.Video.channel_handle == handle,
            )
            .values(source_id=src.id)
        )
        self.audit.record(
            "source.added",
            "source",
            src.id,
            self.actor,
            platform=platform.value,
            channel=handle,
            policy=clip_policy.value,
            videos_linked=int(getattr(res, "rowcount", 0) or 0),
        )
        return src

    def update(self, source_id: UUID, **fields: Any) -> m.Source:
        src = self.repo.get(source_id)
        if src is None:
            raise LookupError("source not found")
        bad = set(fields) - EDITABLE
        if bad:
            raise ValueError(f"unknown fields: {sorted(bad)}")
        if "clip_policy" in fields:
            fields["clip_policy"] = ClipPolicy(fields["clip_policy"]).value
        changes = {k: v for k, v in fields.items() if getattr(src, k) != v}
        for k, v in changes.items():
            setattr(src, k, v)
        self.session.flush()
        if changes:
            self.audit.record(
                "source.updated", "source", src.id, self.actor, changed=sorted(changes)
            )
        return src

    def list(self, limit: int = 100, offset: int = 0) -> Sequence[m.Source]:
        return self.repo.list(limit=limit, offset=offset)

    def retire(self, source_id: UUID, reason: str = "") -> int:
        """Takedown: block the source and hide every clip cut from its videos.
        Returns the number of clips hidden."""
        src = self.repo.get(source_id)
        if src is None:
            raise LookupError("source not found")
        now = datetime.now(UTC)
        src.status, src.retired_at = SourceStatus.RETIRED.value, now

        # Match by source_id AND by channel, so videos ingested via override are covered too.
        channel_match = [m.Video.channel_handle == src.channel_handle]
        if src.channel_id:
            channel_match.append(m.Video.channel_id == src.channel_id)
        video_filter = (
            m.Video.workspace_id == self.workspace_id,
            or_(
                m.Video.source_id == src.id,
                and_(m.Video.platform == src.platform, or_(*channel_match)),
            ),
        )
        self.session.execute(update(m.Video).where(*video_filter).values(status="source_retired"))
        video_ids = select(m.Video.id).where(*video_filter)
        cand_ids = select(m.Candidate.id).where(
            m.Candidate.workspace_id == self.workspace_id, m.Candidate.video_id.in_(video_ids)
        )
        res = self.session.execute(
            update(m.Clip)
            .where(
                m.Clip.workspace_id == self.workspace_id,
                m.Clip.candidate_id.in_(cand_ids),
                m.Clip.deleted_at.is_(None),
            )
            .values(deleted_at=now)
        )
        hidden = int(getattr(res, "rowcount", 0) or 0)
        self.session.flush()
        self.audit.record(
            "source.retired", "source", src.id, self.actor, reason=reason, clips_hidden=hidden
        )
        return hidden
