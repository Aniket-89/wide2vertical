"""M2 ports: chat replay, audio analysis, signals, candidates, and their persistence."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

import numpy as np
import numpy.typing as npt

from clipforge.domain.values import Platform, WorkspaceId
from clipforge.ports.pipeline import (
    PipelineError,
    PipelineStore,
    ResolvedVideo,
    VideoRecord,
    Word,
)

FloatArray = npt.NDArray[np.float64]


class ChatUnavailable(PipelineError):
    """Chat replay could not be fetched. Never fatal: the pipeline continues without it."""


# ---------------------------------------------------------------- chat
@dataclass(frozen=True, slots=True)
class ChatMessage:
    t_s: float  # seconds from the start of the VOD
    author: str  # hashed, never the raw username (privacy; see ADR-0022)
    text: str
    emotes: tuple[str, ...] = ()


class ChatProvider(Protocol):
    platform: Platform

    def fetch_replay(self, video: ResolvedVideo) -> Iterator[ChatMessage]:
        """Yield messages in time order. Raise ChatUnavailable on failure."""
        ...


# ---------------------------------------------------------------- audio
class AudioAnalyzer(Protocol):
    @property
    def version(self) -> str: ...

    def rms_db_per_second(self, audio: Path) -> FloatArray:
        """Loudness (RMS, dBFS) for each 1 s bin. Streams; must not load hours into RAM."""
        ...


# ---------------------------------------------------------------- signals
@dataclass(frozen=True)
class SignalContext:
    """Everything a signal may look at. Built once per video by the analysis service."""

    n_seconds: int
    words: Sequence[Word]
    chat: Sequence[ChatMessage] | None  # None = chat unavailable (not "zero messages")
    audio_rms_db: FloatArray | None
    params: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SignalSeries:
    name: str
    version: str
    values: FloatArray  # normalised 0..1, one value per second
    meta: Mapping[str, Any] = field(default_factory=dict)


class SignalExtractor(Protocol):
    name: str
    version: str
    required: bool

    def extract(self, ctx: SignalContext) -> SignalSeries | None:
        """Return None when the inputs this signal needs are unavailable."""
        ...


# ---------------------------------------------------------------- candidates
@dataclass(frozen=True)
class CandidateWindow:
    start_s: float
    end_s: float
    peak_s: float
    score: float
    components: Mapping[str, float]  # per-signal score inside the window, for "why"
    reason: str


@dataclass(frozen=True, slots=True)
class ScoringProfileRecord:
    id: UUID
    name: str
    version: int
    weights: Mapping[str, float]
    params: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class StoredCandidate:
    id: UUID
    rank: int
    start_s: float
    end_s: float
    score: float
    components: Mapping[str, Any]
    reason: str
    status: str
    title_suggestions: tuple[str, ...] = ()
    hook_text: str = ""
    judge: Mapping[str, Any] | None = None


class AnalysisStore(Protocol):
    workspace_id: WorkspaceId

    def get_video(self, video_id: UUID) -> VideoRecord | None: ...

    def get_transcript(self, video_id: UUID) -> tuple[str, Sequence[Word]] | None:
        """Latest transcript as (content_key, words), or None."""
        ...

    def get_scoring_profile(self, name: str | None) -> ScoringProfileRecord:
        """Named profile (latest version), or the workspace default when name is None."""
        ...

    def find_signal(self, video_id: UUID, name: str, input_hash: str) -> SignalSeries | None: ...

    def save_signal(self, video_id: UUID, s: SignalSeries, input_hash: str) -> None: ...

    def replace_candidates(
        self, video_id: UUID, profile_id: UUID, windows: Sequence[CandidateWindow]
    ) -> int:
        """Replace this profile's *pending* candidates; reviewed ones are kept."""
        ...

    def list_candidates(
        self, video_id: UUID, profile_id: UUID | None = None, limit: int = 100
    ) -> Sequence[StoredCandidate]: ...

    def apply_judgement(
        self,
        candidate_id: UUID,
        *,
        score: float,
        components: Mapping[str, Any],
        judge: Mapping[str, Any],
        titles: Sequence[str],
        hook: str,
        reason: str,
    ) -> None: ...

    def rerank(self, video_id: UUID, profile_id: UUID) -> None:
        """Renumber this profile's candidates 1..n by score (after judging)."""
        ...


class AnalysisPipelineStore(PipelineStore, AnalysisStore, Protocol):
    """What AnalyzePipeline needs: stage tracking/assets/audit plus analysis persistence."""
