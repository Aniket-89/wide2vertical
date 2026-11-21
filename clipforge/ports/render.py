"""M4 ports: source sections, facecam analysis, rendering, and render/review persistence."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from clipforge.ports.analysis import AnalysisStore
from clipforge.ports.pipeline import PipelineError, PipelineStore, ResolvedVideo
from clipforge.rendering.layout import FrameFaces, LayoutPlan


class RenderFailed(PipelineError):
    pass


# ---------------------------------------------------------------- media
@dataclass(frozen=True, slots=True)
class SectionFile:
    """A local video file covering at least [requested start, end] of the VOD."""

    path: Path
    start_s: float  # VOD time at which this file starts (file time 0)
    duration_s: float


class SectionFetcher(Protocol):
    def fetch_section(
        self, video: ResolvedVideo, start_s: float, end_s: float, workdir: Path, max_height: int
    ) -> SectionFile: ...


class FacecamDetector(Protocol):
    def analyze(self, video: Path, times_s: Sequence[float]) -> FrameFaces:
        """Detect faces in frames at the given file-relative times."""
        ...


@dataclass(frozen=True, slots=True)
class RenderSpec:
    source: Path
    offset_s: float  # where the clip starts inside ``source``
    duration_s: float
    plan: LayoutPlan
    ass: str  # captions/credit/intro document (built for out_w x out_h)
    out_w: int
    out_h: int
    quality: str  # "preview" | "final"
    output: Path


@dataclass(frozen=True, slots=True)
class RenderResult:
    duration_s: float
    render_seconds: float


class Renderer(Protocol):
    @property
    def version(self) -> str: ...

    def render(self, spec: RenderSpec) -> RenderResult: ...


# ---------------------------------------------------------------- persistence
@dataclass(frozen=True, slots=True)
class CandidateRecord:
    id: UUID
    video_id: UUID
    scoring_profile_id: UUID
    start_s: float
    end_s: float
    score: float
    rank: int
    components: Mapping[str, Any]
    status: str
    title_suggestions: tuple[str, ...] = ()
    hook_text: str = ""
    reason: str = ""
    judge: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ClipRecord:
    id: UUID
    candidate_id: UUID
    video_id: UUID
    start_s: float
    end_s: float
    layout: str  # "auto" or one of LAYOUTS
    caption_style_id: UUID | None
    caption_override: Mapping[str, Any] | None
    crop_override: Mapping[str, Any] | None
    commentary_text: str
    credit_text: str


@dataclass(frozen=True, slots=True)
class RenderRecord:
    id: UUID
    clip_id: UUID
    kind: str
    render_hash: str
    blob_key: str | None
    status: str
    duration_s: float | None = None
    render_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class CaptionStyleRecord:
    id: UUID
    name: str
    spec: Mapping[str, Any] = field(default_factory=dict)


class RenderStore(Protocol):
    def get_candidate(self, candidate_id: UUID) -> CandidateRecord | None: ...

    def set_candidate_status(self, candidate_id: UUID, status: str) -> None: ...

    def get_clip(self, clip_id: UUID) -> ClipRecord | None: ...

    def get_clip_for_candidate(self, candidate_id: UUID) -> ClipRecord | None: ...

    def create_clip(self, candidate: CandidateRecord, credit_text: str) -> ClipRecord: ...

    def update_clip(self, clip_id: UUID, **fields: Any) -> ClipRecord: ...

    def get_caption_style(self, style_id: UUID | None) -> CaptionStyleRecord:
        """The given style, or the workspace default when None."""
        ...

    def source_crop_override(self, source_id: UUID | None) -> Mapping[str, Any] | None: ...

    def find_render(self, clip_id: UUID, kind: str, render_hash: str) -> RenderRecord | None: ...

    def latest_render(self, clip_id: UUID, kind: str) -> RenderRecord | None: ...

    def save_render(
        self,
        clip_id: UUID,
        kind: str,
        render_hash: str,
        *,
        status: str,
        blob_key: str | None = None,
        duration_s: float | None = None,
        render_seconds: float | None = None,
    ) -> RenderRecord: ...

    def record_feedback(
        self, kind: str, candidate_id: UUID | None, clip_id: UUID | None, **payload: Any
    ) -> None: ...


class RenderPipelineStore(PipelineStore, AnalysisStore, RenderStore, Protocol):
    """What RenderPipeline/ReviewService need."""
