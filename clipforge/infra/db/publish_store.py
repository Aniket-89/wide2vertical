"""SQL implementation of PublishStore (targets, publications) on top of SqlRenderStore."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select, update

from clipforge.infra.db import models as m
from clipforge.infra.db.render_store import SqlRenderStore
from clipforge.ports.publish import PublicationRecord, TargetRecord

PUB_FIELDS = {
    "title", "description", "privacy", "scheduled_at", "published_at", "external_id", "error",
}  # fmt: skip


def target_record(t: m.PublishTarget) -> TargetRecord:
    return TargetRecord(t.id, t.platform, t.account_label, t.secret_ref, t.enabled)


def publication_record(p: m.Publication) -> PublicationRecord:
    return PublicationRecord(
        p.id,
        p.clip_id,
        p.publish_target_id,
        p.status,
        p.title,
        p.description,
        p.privacy,
        p.scheduled_at,
        p.published_at,
        p.external_id,
        p.error,
        p.idempotency_key,
    )


class SqlPublishStore(SqlRenderStore):
    # -- targets
    def get_target(self, target_id: UUID) -> TargetRecord | None:
        t = self.session.get(m.PublishTarget, target_id)
        return target_record(t) if t is not None and t.workspace_id == self.workspace_id else None

    def default_target(self, platform: str) -> TargetRecord | None:
        t = self.session.scalars(
            select(m.PublishTarget)
            .where(
                m.PublishTarget.workspace_id == self.workspace_id,
                m.PublishTarget.platform == platform,
                m.PublishTarget.enabled.is_(True),
            )
            .order_by(m.PublishTarget.created_at)
        ).first()
        return target_record(t) if t else None

    def list_targets(self) -> list[TargetRecord]:
        rows = self.session.scalars(
            select(m.PublishTarget)
            .where(m.PublishTarget.workspace_id == self.workspace_id)
            .order_by(m.PublishTarget.created_at)
        )
        return [target_record(t) for t in rows]

    def add_target(self, platform: str, label: str, secret_ref: str | None) -> TargetRecord:
        t = m.PublishTarget(
            workspace_id=self.workspace_id,
            platform=platform,
            account_label=label[:200],
            secret_ref=secret_ref,
            enabled=True,
        )
        self.session.add(t)
        self.session.flush()
        self.audit("publish_target.added", "publish_target", t.id, platform=platform)
        return target_record(t)

    # -- publications
    def _pub(self, pub_id: UUID) -> m.Publication | None:
        p = self.session.get(m.Publication, pub_id)
        return p if p is not None and p.workspace_id == self.workspace_id else None

    def get_publication(self, pub_id: UUID) -> PublicationRecord | None:
        p = self._pub(pub_id)
        if p is not None:
            self.session.refresh(p)  # transition() updates with core SQL
        return publication_record(p) if p else None

    def find_publication(self, idempotency_key: str) -> PublicationRecord | None:
        p = self.session.scalars(
            select(m.Publication).where(
                m.Publication.workspace_id == self.workspace_id,
                m.Publication.idempotency_key == idempotency_key,
            )
        ).first()
        return publication_record(p) if p else None

    def create_publication(
        self,
        clip_id: UUID,
        target_id: UUID,
        *,
        title: str,
        description: str,
        privacy: str,
        scheduled_at: datetime,
        idempotency_key: str,
    ) -> PublicationRecord:
        p = m.Publication(
            workspace_id=self.workspace_id,
            clip_id=clip_id,
            publish_target_id=target_id,
            status="scheduled",
            title=title,
            description=description,
            privacy=privacy,
            scheduled_at=scheduled_at,
            idempotency_key=idempotency_key,
        )
        self.session.add(p)
        self.session.flush()
        return publication_record(p)

    def transition(
        self, pub_id: UUID, from_statuses: Sequence[str], to_status: str, **fields: Any
    ) -> bool:
        unknown = set(fields) - PUB_FIELDS
        if unknown:
            raise ValueError(f"not publication fields: {sorted(unknown)}")
        res = self.session.execute(
            update(m.Publication)
            .where(
                m.Publication.workspace_id == self.workspace_id,
                m.Publication.id == pub_id,
                m.Publication.status.in_(list(from_statuses)),
            )
            .values(status=to_status, **fields)
            .execution_options(synchronize_session=False)
        )
        self.session.flush()
        return bool(res.rowcount)  # type: ignore[attr-defined]

    def due_publications(self, now: datetime, limit: int = 20) -> list[PublicationRecord]:
        rows = self.session.scalars(
            select(m.Publication)
            .where(
                m.Publication.workspace_id == self.workspace_id,
                m.Publication.status == "scheduled",
                m.Publication.scheduled_at <= now,
            )
            .order_by(m.Publication.scheduled_at)
            .limit(limit)
        )
        return [publication_record(p) for p in rows]

    def published_since(self, since: datetime, limit: int = 200) -> list[PublicationRecord]:
        rows = self.session.scalars(
            select(m.Publication)
            .where(
                m.Publication.workspace_id == self.workspace_id,
                m.Publication.status == "published",
                m.Publication.published_at >= since,
            )
            .order_by(m.Publication.published_at.desc())
            .limit(limit)
        )
        return [publication_record(p) for p in rows]

    def list_publications(
        self, clip_id: UUID | None = None, limit: int = 100
    ) -> list[PublicationRecord]:
        stmt = select(m.Publication).where(m.Publication.workspace_id == self.workspace_id)
        if clip_id is not None:
            stmt = stmt.where(m.Publication.clip_id == clip_id)
        rows = self.session.scalars(stmt.order_by(m.Publication.id.desc()).limit(limit))
        return [publication_record(p) for p in rows]

    def latest_performance(self, pub_ids: Sequence[UUID]) -> dict[UUID, dict[str, Any]]:
        """Newest pulled stats per publication (feedback_events kind=performance)."""
        if not pub_ids:
            return {}
        wanted = {str(i) for i in pub_ids}
        rows = self.session.scalars(
            select(m.FeedbackEvent)
            .where(
                m.FeedbackEvent.workspace_id == self.workspace_id,
                m.FeedbackEvent.kind == "performance",
                m.FeedbackEvent.payload["publication_id"].astext.in_(wanted),
            )
            .order_by(m.FeedbackEvent.occurred_at.desc(), m.FeedbackEvent.id.desc())
        )
        out: dict[UUID, dict[str, Any]] = {}
        for ev in rows:
            pid = UUID(ev.payload["publication_id"])
            if pid not in out:
                out[pid] = {
                    "views": ev.payload.get("views"),
                    "likes": ev.payload.get("likes"),
                    "comments": ev.payload.get("comments"),
                    "at": ev.occurred_at,
                }
        return out
