"""Pipeline ports for M1: resolve → fetch → transcribe, plus the persistence the
services need. Services see only these Protocols and the dataclasses below.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol
from uuid import UUID

from clipforge.domain.values import (
    BlobKey,
    ClipPolicy,
    JobStatus,
    Platform,
    SourceStatus,
    Stage,
    WorkspaceId,
)


# ---------------------------------------------------------------- errors
class PipelineError(Exception):
    """Base for expected, user-facing pipeline failures."""


class UnsupportedUrl(PipelineError):
    pass


class ResolveFailed(PipelineError):
    pass


class FetchFailed(PipelineError):
    pass


class TranscribeFailed(PipelineError):
    pass


# ---------------------------------------------------------------- value types
@dataclass(frozen=True, slots=True)
class ResolvedVideo:
    platform: Platform
    url: str
    external_id: str
    channel_handle: str  # normalised: lowercase, no leading "@"
    channel_id: str | None
    title: str
    duration_s: float | None
    is_live: bool = False


@dataclass(frozen=True, slots=True)
class FetchedFile:
    path: Path
    content_hash: str  # sha256 hex
    bytes: int
    ext: str
    duration_s: float | None = None  # measured from the file, None if unknown


# A fetched file shorter than this fraction of the platform-reported duration is truncated.
MIN_COMPLETENESS = 0.98


@dataclass(frozen=True, slots=True)
class Word:
    text: str
    start: float
    end: float
    prob: float


@dataclass(frozen=True, slots=True)
class Transcript:
    language: str | None
    duration_s: float
    model: str
    words: Sequence[Word] = field(default_factory=tuple)


# ---------------------------------------------------------------- external-system ports
class SourceResolver(Protocol):
    platform: Platform

    def supports(self, url: str) -> bool: ...

    def resolve(self, url: str) -> ResolvedVideo: ...


class MediaFetcher(Protocol):
    def fetch_audio(self, video: ResolvedVideo, workdir: Path) -> FetchedFile: ...


class Transcriber(Protocol):
    @property
    def config_version(self) -> str:
        """Changes whenever output could change (model, compute type, options)."""
        ...

    def transcribe(self, audio: Path) -> Transcript: ...


# ---------------------------------------------------------------- persistence port
@dataclass(frozen=True, slots=True)
class SourceRecord:
    id: UUID
    platform: Platform
    channel_handle: str
    clip_policy: ClipPolicy
    status: SourceStatus
    required_credit_text: str


@dataclass(frozen=True, slots=True)
class VideoRecord:
    id: UUID
    workspace_id: WorkspaceId
    source_id: UUID | None
    platform: Platform
    url: str
    external_id: str
    status: str
    duration_s: float | None


@dataclass(frozen=True, slots=True)
class AssetRecord:
    blob_key: BlobKey
    content_hash: str
    bytes: int


class PipelineStore(Protocol):
    """Everything the ingest pipeline persists. Implementations are workspace-scoped."""

    workspace_id: WorkspaceId

    def find_source(
        self, platform: Platform, channel_handle: str, channel_id: str | None
    ) -> SourceRecord | None: ...

    def upsert_video(
        self, rv: ResolvedVideo, source_id: UUID | None, override_used: bool
    ) -> tuple[VideoRecord, bool]:
        """Insert or update by (platform, external_id). Returns (video, created)."""
        ...

    def get_video(self, video_id: UUID) -> VideoRecord | None: ...

    def set_video_status(self, video_id: UUID, status: str) -> None: ...

    def find_asset(self, video_id: UUID, kind: str) -> AssetRecord | None: ...

    def save_asset(self, video_id: UUID, kind: str, asset: AssetRecord) -> None: ...

    def has_transcript(self, video_id: UUID, content_hash: str, config_version: str) -> bool: ...

    def save_transcript(
        self, video_id: UUID, content_hash: str, config_version: str, t: Transcript
    ) -> None: ...

    def begin_stage(self, video_id: UUID, stage: Stage, input_hash: str) -> UUID: ...

    def end_stage(self, run_id: UUID, status: JobStatus, error: str | None = None) -> None: ...

    def record_usage(
        self, kind: str, quantity: float, unit: str, video_id: UUID | None, **meta: object
    ) -> None: ...

    def audit(
        self, action: str, target_type: str, target_id: UUID | None, **details: object
    ) -> None: ...
