"""Workspace-scoped repositories. There is deliberately no way to query a business
table without a workspace: ``WorkspaceRepository`` bakes ``workspace_id`` into every
statement, and refuses to write rows belonging to another workspace.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, select
from sqlalchemy.orm import Session

from clipforge.domain.values import WorkspaceId
from clipforge.infra.db import models as m


class CrossWorkspaceWrite(PermissionError):
    pass


class WorkspaceRepository[T: m.WorkspaceScoped]:
    model: type[T]

    def __init__(self, session: Session, workspace_id: WorkspaceId) -> None:
        if workspace_id is None:
            raise ValueError("workspace_id is required")
        self.session = session
        self.workspace_id = workspace_id

    # -- query building: the only entry point for reads
    def _select(self, *, include_deleted: bool = False) -> Select[T]:
        stmt = select(self.model).where(self.model.workspace_id == self.workspace_id)
        if not include_deleted and hasattr(self.model, "deleted_at"):
            stmt = stmt.where(self.model.deleted_at.is_(None))  # type: ignore[attr-defined]
        return stmt

    def get(self, id_: UUID, *, include_deleted: bool = False) -> T | None:
        stmt = self._select(include_deleted=include_deleted).where(self.model.id == id_)
        return self.session.scalars(stmt).one_or_none()

    def list(
        self, *, limit: int = 50, offset: int = 0, include_deleted: bool = False, **filters: Any
    ) -> Sequence[T]:
        stmt = self._select(include_deleted=include_deleted)
        for col, val in filters.items():
            stmt = stmt.where(getattr(self.model, col) == val)
        stmt = stmt.order_by(self.model.id.desc()).limit(limit).offset(offset)  # uuid7 ~ time
        return self.session.scalars(stmt).all()

    # -- writes
    def add(self, obj: T) -> T:
        if obj.workspace_id is None:
            obj.workspace_id = self.workspace_id
        elif obj.workspace_id != self.workspace_id:
            raise CrossWorkspaceWrite(f"{self.model.__name__} belongs to another workspace")
        self.session.add(obj)
        self.session.flush()
        return obj

    def soft_delete(self, id_: UUID) -> bool:
        obj = self.get(id_)
        if obj is None or not hasattr(obj, "deleted_at"):
            return False
        obj.deleted_at = datetime.now(UTC)
        self.session.flush()
        return True


class SourceRepo(WorkspaceRepository[m.Source]):
    model = m.Source

    def find(self, platform: str, channel_handle: str) -> m.Source | None:
        stmt = self._select().where(
            m.Source.platform == platform,
            m.Source.channel_handle == channel_handle.lower(),
        )
        return self.session.scalars(stmt).one_or_none()


class VideoRepo(WorkspaceRepository[m.Video]):
    model = m.Video


class CandidateRepo(WorkspaceRepository[m.Candidate]):
    model = m.Candidate


class ClipRepo(WorkspaceRepository[m.Clip]):
    model = m.Clip


class ScoringProfileRepo(WorkspaceRepository[m.ScoringProfile]):
    model = m.ScoringProfile


class CaptionStyleRepo(WorkspaceRepository[m.CaptionStyle]):
    model = m.CaptionStyle


class JobRepo(WorkspaceRepository[m.Job]):
    model = m.Job


class AuditRepo(WorkspaceRepository[m.AuditLog]):
    model = m.AuditLog

    def record(
        self,
        action: str,
        target_type: str,
        target_id: UUID | None,
        actor_user_id: UUID | None = None,
        **details: Any,
    ) -> m.AuditLog:
        return self.add(
            m.AuditLog(
                workspace_id=self.workspace_id,
                actor_user_id=actor_user_id,
                action=action,
                target_type=target_type,
                target_id=target_id,
                details=details,
            )
        )


class UsageRepo(WorkspaceRepository[m.UsageEvent]):
    model = m.UsageEvent


ALL_SCOPED_REPOS: tuple[type[WorkspaceRepository[Any]], ...] = (
    SourceRepo,
    VideoRepo,
    CandidateRepo,
    ClipRepo,
    ScoringProfileRepo,
    CaptionStyleRepo,
    JobRepo,
    AuditRepo,
    UsageRepo,
)
