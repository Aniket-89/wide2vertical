"""Value objects and enums. Pure Python: no I/O, no framework imports."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import NewType
from uuid import UUID

WorkspaceId = NewType("WorkspaceId", UUID)
UserId = NewType("UserId", UUID)


class Platform(StrEnum):
    YOUTUBE = "youtube"
    TWITCH = "twitch"
    KICK = "kick"


class ClipPolicy(StrEnum):
    CAMPAIGN = "campaign"
    WRITTEN_PERMISSION = "written_permission"
    OPEN_POLICY = "open_policy"
    OWN_CHANNEL = "own_channel"
    UNKNOWN = "unknown"


class SourceStatus(StrEnum):
    ACTIVE = "active"
    RETIRED = "retired"


class Role(StrEnum):
    OWNER = "owner"
    EDITOR = "editor"
    VIEWER = "viewer"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    SKIPPED = "skipped"


class Stage(StrEnum):
    INGEST = "ingest"
    FETCH = "fetch"
    TRANSCRIBE = "transcribe"
    SIGNALS = "signals"
    CANDIDATES = "candidates"
    JUDGE = "judge"
    RENDER = "render"
    PUBLISH = "publish"


@dataclass(frozen=True, slots=True)
class TimeRange:
    """Half-open interval [start_s, end_s) in seconds from the start of the video."""

    start_s: float
    end_s: float

    def __post_init__(self) -> None:
        if self.start_s < 0:
            raise ValueError("start_s must be >= 0")
        if self.end_s <= self.start_s:
            raise ValueError("end_s must be > start_s")

    @property
    def duration_s(self) -> float:
        return self.end_s - self.start_s

    def overlap_s(self, other: TimeRange) -> float:
        return max(0.0, min(self.end_s, other.end_s) - max(self.start_s, other.start_s))


_SAFE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass(frozen=True, slots=True)
class BlobKey:
    """A storage key. Always workspace-prefixed: ``ws/{workspace_id}/{segments...}``.

    Build with :meth:`for_workspace`; the constructor validates the shape so a
    key without a workspace prefix cannot exist.
    """

    value: str

    def __post_init__(self) -> None:
        parts = self.value.split("/")
        if len(parts) < 3 or parts[0] != "ws":
            raise ValueError(f"BlobKey must start with ws/<workspace_id>/: {self.value!r}")
        UUID(parts[1])  # raises if not a UUID
        for seg in parts[2:]:
            if not _SAFE_SEGMENT.match(seg) or seg in {".", ".."}:
                raise ValueError(f"unsafe BlobKey segment {seg!r}")

    @classmethod
    def for_workspace(cls, workspace_id: WorkspaceId, *segments: str) -> BlobKey:
        return cls("/".join(["ws", str(workspace_id), *segments]))

    @property
    def workspace_id(self) -> WorkspaceId:
        return WorkspaceId(UUID(self.value.split("/")[1]))


@dataclass(frozen=True, slots=True)
class Principal:
    """Who is making a request, and in which workspace. Returned by the auth seam."""

    user_id: UserId
    workspace_id: WorkspaceId
    role: Role


def normalise_handle(raw: str) -> str:
    """Canonical channel handle: trimmed, no leading '@', lowercase."""
    return raw.strip().lstrip("@").lower()
