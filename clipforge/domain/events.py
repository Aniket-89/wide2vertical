"""Domain events. Modules communicate through these, never through each other's tables."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID

from clipforge.domain.values import Stage, WorkspaceId


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, kw_only=True)
class DomainEvent:
    workspace_id: WorkspaceId
    occurred_at: datetime = field(default_factory=_utcnow)


@dataclass(frozen=True, kw_only=True)
class VideoIngested(DomainEvent):
    video_id: UUID
    override_used: bool


@dataclass(frozen=True, kw_only=True)
class StageCompleted(DomainEvent):
    video_id: UUID
    stage: Stage


@dataclass(frozen=True, kw_only=True)
class StageFailed(DomainEvent):
    video_id: UUID
    stage: Stage
    error: str


@dataclass(frozen=True, kw_only=True)
class SourceRetired(DomainEvent):
    source_id: UUID
