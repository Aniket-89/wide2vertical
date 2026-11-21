"""M6 ports: publishing clips to platforms, sealing their credentials, and the store behind it."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Protocol
from uuid import UUID

from clipforge.domain.values import WorkspaceId
from clipforge.ports.pipeline import PipelineError
from clipforge.ports.render import RenderPipelineStore

PRIVACY = ("private", "unlisted", "public")
# scheduled -> queued -> uploading -> published | failed;  scheduled -> cancelled
PUB_STATUSES = ("scheduled", "queued", "uploading", "published", "failed", "cancelled")


class PublishError(PipelineError):
    """Publishing failed; the message is safe to show and store (no tokens)."""


class PublishNotSupported(PublishError):
    """The platform adapter is a stub or disabled by a feature flag."""


class PublishRefused(PipelineError):
    """A policy check refused the publish (not approved, retired source, no final render)."""


@dataclass(frozen=True, slots=True)
class PublishRequest:
    path: Path  # local MP4 (final render)
    title: str
    description: str
    privacy: str = "private"
    tags: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PublishResult:
    external_id: str
    url: str


@dataclass(frozen=True, slots=True)
class VideoStats:
    views: int
    likes: int | None = None
    comments: int | None = None
    extra: Mapping[str, Any] = field(default_factory=dict)


class Publisher(Protocol):
    platform: ClassVar[str]

    def publish(self, req: PublishRequest, credentials: bytes) -> PublishResult:
        """Upload and return the platform id. ``credentials`` are the opened secret."""
        ...

    def stats(self, external_ids: Sequence[str], credentials: bytes) -> dict[str, VideoStats]:
        """Current public counters for already-published videos (missing ids omitted)."""
        ...


class SecretBox(Protocol):
    """Encrypts per-workspace secrets (OAuth refresh tokens). A ref from one workspace must
    not open under another."""

    def seal(self, ws: WorkspaceId, plaintext: bytes) -> str: ...

    def open(self, ws: WorkspaceId, ref: str) -> bytes: ...


@dataclass(frozen=True, slots=True)
class TargetRecord:
    id: UUID
    platform: str
    account_label: str
    secret_ref: str | None
    enabled: bool


@dataclass(frozen=True, slots=True)
class PublicationRecord:
    id: UUID
    clip_id: UUID
    target_id: UUID
    status: str
    title: str
    description: str
    privacy: str
    scheduled_at: datetime | None
    published_at: datetime | None
    external_id: str | None
    error: str | None
    idempotency_key: str


class PublishStore(Protocol):
    workspace_id: WorkspaceId

    def get_target(self, target_id: UUID) -> TargetRecord | None: ...

    def default_target(self, platform: str) -> TargetRecord | None: ...

    def get_publication(self, pub_id: UUID) -> PublicationRecord | None: ...

    def find_publication(self, idempotency_key: str) -> PublicationRecord | None: ...

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
    ) -> PublicationRecord: ...

    def transition(
        self, pub_id: UUID, from_statuses: Sequence[str], to_status: str, **fields: Any
    ) -> bool:
        """Atomically move a publication between statuses (compare-and-set). False when the
        row wasn't in one of ``from_statuses`` (someone else got there first)."""
        ...

    def due_publications(self, now: datetime, limit: int = 20) -> list[PublicationRecord]: ...

    def published_since(self, since: datetime, limit: int = 200) -> list[PublicationRecord]: ...


class PublishPipelineStore(RenderPipelineStore, PublishStore, Protocol):
    """What PublishService needs: clips, candidates, sources, renders, feedback, plus
    publications and targets."""
