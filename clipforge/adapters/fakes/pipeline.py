"""Fakes for pipeline ports. Deterministic, no network, with failure injection."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from uuid import UUID

import numpy as np

from clipforge.adapters.resolvers.urls import parse_video_url
from clipforge.domain.ids import new_id
from clipforge.domain.values import (
    ClipPolicy,
    JobStatus,
    Platform,
    SourceStatus,
    Stage,
    WorkspaceId,
    normalise_handle,
)
from clipforge.ports.analysis import (
    CandidateWindow,
    ChatMessage,
    ChatUnavailable,
    FloatArray,
    ScoringProfileRecord,
    SignalSeries,
    StoredCandidate,
)
from clipforge.ports.judge import (
    JudgeInput,
    JudgeResult,
    JudgeUnavailable,
    JudgeUsage,
    JudgeVerdict,
)
from clipforge.ports.pipeline import (
    AssetRecord,
    FetchedFile,
    FetchFailed,
    PipelineError,
    ResolvedVideo,
    ResolveFailed,
    SourceRecord,
    TranscribeFailed,
    Transcript,
    UnsupportedUrl,
    VideoRecord,
    Word,
)
from clipforge.ports.render import (
    CandidateRecord,
    CaptionStyleRecord,
    ClipRecord,
    RenderFailed,
    RenderRecord,
    RenderResult,
    RenderSpec,
    SectionFile,
)
from clipforge.rendering.layout import FrameFaces


class FakeResolver:
    """Resolves any supported URL for ``platform``. Channel comes from ``channels[url]``
    (default "streamer"). ``fail=True`` simulates the platform being unreachable."""

    def __init__(
        self,
        platform: Platform,
        channels: dict[str, str] | None = None,
        duration_s: float = 60.0,
        fail: bool = False,
    ) -> None:
        self.platform = platform
        self.channels = channels or {}
        self.duration_s = duration_s
        self.fail = fail
        self.calls = 0

    def supports(self, url: str) -> bool:
        p = parse_video_url(url)
        return p is not None and p.platform is self.platform

    def resolve(self, url: str) -> ResolvedVideo:
        self.calls += 1
        p = parse_video_url(url)
        if p is None or p.platform is not self.platform:
            raise UnsupportedUrl(url)
        if self.fail:
            raise ResolveFailed("fake: platform unreachable")
        return ResolvedVideo(
            platform=self.platform,
            url=url,
            external_id=p.external_id,
            channel_handle=normalise_handle(self.channels.get(url, "streamer")),
            channel_id=None,
            title=f"fake {p.external_id}",
            duration_s=self.duration_s,
        )


class FakeFetcher:
    def __init__(
        self, payload: bytes = b"fake-audio", fail: bool = False, duration_s: float | None = None
    ) -> None:
        self.payload = payload
        self.fail = fail
        self.duration_s = duration_s  # what the "file" measures; None = unknown
        self.calls = 0

    def fetch_audio(self, video: ResolvedVideo, workdir: Path) -> FetchedFile:
        self.calls += 1
        if self.fail:
            raise FetchFailed("fake: download failed")
        f = workdir / "audio.m4a"
        f.write_bytes(self.payload + video.external_id.encode())
        data = f.read_bytes()
        return FetchedFile(
            f, hashlib.sha256(data).hexdigest(), len(data), "m4a", duration_s=self.duration_s
        )


DEFAULT_WORDS: tuple[Word, ...] = (
    Word("no", 1.0, 1.2, 0.99),
    Word("way", 1.2, 1.5, 0.99),
    Word("that", 1.6, 1.8, 0.98),
    Word("just", 1.8, 2.0, 0.97),
    Word("happened!", 2.0, 2.6, 0.95),
)


class FakeTranscriber:
    def __init__(
        self,
        words: Sequence[Word] = DEFAULT_WORDS,
        fail: Exception | None = None,
        version: str = "fake-1",
    ) -> None:
        self.words = tuple(words)
        self.fail = fail
        self.version = version
        self.calls = 0

    @property
    def config_version(self) -> str:
        return self.version

    def transcribe(self, audio: Path) -> Transcript:
        self.calls += 1
        if not audio.exists():
            raise TranscribeFailed(f"missing audio {audio}")
        if self.fail is not None:
            raise self.fail
        return Transcript(language="en", duration_s=60.0, model="fake", words=self.words)


@dataclass
class _Source:
    rec: SourceRecord
    channel_id: str | None = None


@dataclass
class InMemoryPipelineStore:
    workspace_id: WorkspaceId
    sources: list[_Source] = field(default_factory=list)
    videos: dict[UUID, VideoRecord] = field(default_factory=dict)
    assets: dict[tuple[UUID, str], AssetRecord] = field(default_factory=dict)
    transcripts: dict[tuple[UUID, str, str], Transcript] = field(default_factory=dict)
    jobs: dict[tuple[UUID, str], dict[str, Any]] = field(default_factory=dict)
    runs: dict[UUID, dict[str, Any]] = field(default_factory=dict)
    usage: list[dict[str, Any]] = field(default_factory=list)
    audits: list[dict[str, Any]] = field(default_factory=list)

    # test helper
    def add_source(
        self,
        platform: Platform,
        handle: str,
        policy: ClipPolicy,
        status: SourceStatus = SourceStatus.ACTIVE,
        credit: str = "",
        channel_id: str | None = None,
    ) -> SourceRecord:
        rec = SourceRecord(new_id(), platform, normalise_handle(handle), policy, status, credit)
        self.sources.append(_Source(rec, channel_id))
        return rec

    def find_source(
        self, platform: Platform, channel_handle: str, channel_id: str | None
    ) -> SourceRecord | None:
        for s in self.sources:
            if s.rec.platform is platform and (
                s.rec.channel_handle == channel_handle
                or (channel_id is not None and s.channel_id == channel_id)
            ):
                return s.rec
        return None

    def upsert_video(
        self, rv: ResolvedVideo, source_id: UUID | None, override_used: bool
    ) -> tuple[VideoRecord, bool]:
        for v in self.videos.values():
            if v.platform is rv.platform and v.external_id == rv.external_id:
                nv = replace(
                    v,
                    source_id=source_id,
                    url=rv.url,
                    duration_s=rv.duration_s,
                    channel_handle=rv.channel_handle,
                    channel_id=rv.channel_id,
                )
                self.videos[v.id] = nv
                return nv, False
        v = VideoRecord(
            new_id(),
            self.workspace_id,
            source_id,
            rv.platform,
            rv.url,
            rv.external_id,
            "ingested",
            rv.duration_s,
            rv.channel_handle,
            rv.channel_id,
        )
        self.videos[v.id] = v
        return v, True

    def get_video(self, video_id: UUID) -> VideoRecord | None:
        return self.videos.get(video_id)

    def source_for_video(self, video: VideoRecord) -> SourceRecord | None:
        for s in self.sources:
            if video.source_id is not None and s.rec.id == video.source_id:
                return s.rec
        if video.channel_handle:
            return self.find_source(video.platform, video.channel_handle, video.channel_id)
        return None

    def retire(self, source_id: UUID) -> None:
        """Test helper: mark a source retired (takedown)."""
        for i, s in enumerate(self.sources):
            if s.rec.id == source_id:
                self.sources[i] = _Source(replace(s.rec, status=SourceStatus.RETIRED), s.channel_id)

    def set_video_status(self, video_id: UUID, status: str) -> None:
        self.videos[video_id] = replace(self.videos[video_id], status=status)

    def find_asset(self, video_id: UUID, kind: str) -> AssetRecord | None:
        return self.assets.get((video_id, kind))

    def save_asset(self, video_id: UUID, kind: str, asset: AssetRecord) -> None:
        self.assets[(video_id, kind)] = asset

    def has_transcript(self, video_id: UUID, content_hash: str, config_version: str) -> bool:
        return (video_id, content_hash, config_version) in self.transcripts

    def save_transcript(
        self, video_id: UUID, content_hash: str, config_version: str, t: Transcript
    ) -> None:
        self.transcripts.setdefault((video_id, content_hash, config_version), t)

    def begin_stage(self, video_id: UUID, stage: Stage, input_hash: str) -> UUID:
        job = self.jobs.setdefault((video_id, stage.value), {"attempts": 0})
        job["attempts"] += 1
        job["status"] = JobStatus.RUNNING
        run_id = new_id()
        self.runs[run_id] = {
            "job": (video_id, stage.value),
            "status": JobStatus.RUNNING,
            "attempt": job["attempts"],
            "error": None,
        }
        return run_id

    def end_stage(self, run_id: UUID, status: JobStatus, error: str | None = None) -> None:
        run = self.runs[run_id]
        run["status"], run["error"] = status, error
        self.jobs[run["job"]]["status"] = status

    def record_usage(
        self, kind: str, quantity: float, unit: str, video_id: UUID | None, **meta: object
    ) -> None:
        self.usage.append({"kind": kind, "quantity": quantity, "unit": unit, "video_id": video_id})

    def audit(
        self, action: str, target_type: str, target_id: UUID | None, **details: object
    ) -> None:
        self.audits.append({"action": action, "target_id": target_id, **details})


# ====================================================================== M2 fakes
class FakeChatProvider:
    """Serves fixed messages; ``fail`` simulates the provider being down (e.g. Kick)."""

    def __init__(
        self, platform: Platform, messages: Sequence[ChatMessage] = (), fail: bool = False
    ) -> None:
        self.platform = platform
        self.messages = list(messages)
        self.fail = fail
        self.calls = 0

    def fetch_replay(self, video: ResolvedVideo) -> Iterator[ChatMessage]:
        self.calls += 1
        if self.fail:
            raise ChatUnavailable("fake: chat provider down")
        yield from self.messages


class FakeAudioAnalyzer:
    def __init__(self, rms_db: Sequence[float] | None = None, fail: bool = False) -> None:
        self.rms_db = rms_db
        self.fail = fail
        self.calls = 0

    @property
    def version(self) -> str:
        return "fake-rms-1"

    def rms_db_per_second(self, audio: Path) -> FloatArray:
        self.calls += 1
        if self.fail:
            raise PipelineError("fake: audio decode failed")
        return np.asarray(self.rms_db if self.rms_db is not None else [-30.0] * 60, dtype=float)


@dataclass
class InMemoryAnalysisStore(InMemoryPipelineStore):
    """InMemoryPipelineStore + the M2 AnalysisStore methods."""

    profiles: list[ScoringProfileRecord] = field(default_factory=list)
    signal_rows: dict[tuple[UUID, str, str], SignalSeries] = field(default_factory=dict)
    candidates: dict[UUID, list[StoredCandidate]] = field(default_factory=dict)
    candidate_profile: dict[UUID, UUID] = field(default_factory=dict)

    def get_transcript(self, video_id: UUID) -> tuple[str, Sequence[Word]] | None:
        for (vid, h, cfg), t in reversed(list(self.transcripts.items())):
            if vid == video_id:
                return f"{h}:{cfg}", t.words
        return None

    def get_scoring_profile(self, name: str | None) -> ScoringProfileRecord:
        matches = [p for p in self.profiles if name is None or p.name == name]
        if not matches:
            raise PipelineError(f"scoring profile {name or '(default)'} not found")
        return max(matches, key=lambda p: p.version)

    def find_signal(self, video_id: UUID, name: str, input_hash: str) -> SignalSeries | None:
        return self.signal_rows.get((video_id, name, input_hash))

    def save_signal(self, video_id: UUID, s: SignalSeries, input_hash: str) -> None:
        self.signal_rows.setdefault((video_id, s.name, input_hash), s)

    def replace_candidates(
        self, video_id: UUID, profile_id: UUID, windows: Sequence[CandidateWindow]
    ) -> int:
        current = self.candidates.get(video_id, [])
        keep = [
            c
            for c in current
            if self.candidate_profile.get(c.id) != profile_id or c.status != "pending"
        ]
        kept_bounds = {(c.start_s, c.end_s) for c in keep}
        new = []
        for rank, w in enumerate(windows, start=1):
            if (w.start_s, w.end_s) in kept_bounds:
                continue
            c = StoredCandidate(
                new_id(), rank, w.start_s, w.end_s, w.score, dict(w.components), w.reason, "pending"
            )
            self.candidate_profile[c.id] = profile_id
            new.append(c)
        self.candidates[video_id] = keep + new
        return len(new)

    def list_candidates(
        self, video_id: UUID, profile_id: UUID | None = None, limit: int = 100
    ) -> Sequence[StoredCandidate]:
        rows = [
            c
            for c in self.candidates.get(video_id, [])
            if profile_id is None or self.candidate_profile.get(c.id) == profile_id
        ]
        return sorted(rows, key=lambda c: -c.score)[:limit]

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
    ) -> None:
        for rows in self.candidates.values():
            for i, c in enumerate(rows):
                if c.id == candidate_id:
                    rows[i] = replace(
                        c,
                        score=score,
                        components=dict(components),
                        judge=dict(judge),
                        title_suggestions=tuple(titles),
                        hook_text=hook,
                        reason=reason,
                    )
                    return

    def rerank(self, video_id: UUID, profile_id: UUID) -> None:
        rows = self.candidates.get(video_id, [])
        mine = sorted(
            (c for c in rows if self.candidate_profile.get(c.id) == profile_id),
            key=lambda c: (-c.score, c.start_s),
        )
        new_rank = {c.id: i for i, c in enumerate(mine, start=1)}
        self.candidates[video_id] = [replace(c, rank=new_rank.get(c.id, c.rank)) for c in rows]


class FakeJudge:
    """Scores windows by a function of their transcript; can fail per call or always."""

    def __init__(
        self,
        score_fn: Callable[[JudgeInput], float] | None = None,
        *,
        fail_calls: set[int] | None = None,
        always_fail: bool = False,
    ) -> None:
        self.score_fn = score_fn or (lambda it: 0.5)
        self.fail_calls = fail_calls or set()
        self.always_fail = always_fail
        self.calls = 0
        self.batches: list[list[int]] = []

    @property
    def enabled(self) -> bool:
        return True

    @property
    def model(self) -> str:
        return "fake-judge"

    @property
    def prompt_id(self) -> str:
        return "judge.moment.v1"

    def judge(self, items: Sequence[JudgeInput]) -> JudgeResult:
        self.calls += 1
        self.batches.append([it.index for it in items])
        if self.always_fail or self.calls in self.fail_calls:
            raise JudgeUnavailable("fake: LLM timeout")
        verdicts = [
            JudgeVerdict(
                index=it.index,
                scores={
                    "funny": 0.1,
                    "shocking": 0.1,
                    "clutch": 0.1,
                    "emotional": 0.1,
                    "quotable": 0.1,
                },
                setup_payoff=True,
                overall=self.score_fn(it),
                titles=(f"title {it.index} a", f"title {it.index} b", f"title {it.index} c"),
                hook=f"hook {it.index}",
                reason=f"fake reason {it.index}",
            )
            for it in items
        ]
        return JudgeResult(verdicts, JudgeUsage(input_tokens=100 * len(items), output_tokens=40))


# ====================================================================== M4 fakes
@dataclass
class InMemoryRenderStore(InMemoryAnalysisStore):
    clips: dict[UUID, ClipRecord] = field(default_factory=dict)
    renders: dict[tuple[UUID, str, str], RenderRecord] = field(default_factory=dict)
    feedback: list[dict[str, Any]] = field(default_factory=list)
    styles: list[CaptionStyleRecord] = field(default_factory=list)
    source_crops: dict[UUID, dict[str, Any]] = field(default_factory=dict)

    def _find_candidate(self, candidate_id: UUID) -> tuple[UUID, int, StoredCandidate] | None:
        for vid, rows in self.candidates.items():
            for i, c in enumerate(rows):
                if c.id == candidate_id:
                    return vid, i, c
        return None

    def get_candidate(self, candidate_id: UUID) -> CandidateRecord | None:
        hit = self._find_candidate(candidate_id)
        if hit is None:
            return None
        vid, _, c = hit
        return CandidateRecord(
            c.id,
            vid,
            self.candidate_profile[c.id],
            c.start_s,
            c.end_s,
            c.score,
            c.rank,
            dict(c.components),
            c.status,
            c.title_suggestions,
            c.hook_text,
            c.reason,
            c.judge,
        )

    def set_candidate_status(self, candidate_id: UUID, status: str) -> None:
        hit = self._find_candidate(candidate_id)
        if hit is not None:
            vid, i, c = hit
            self.candidates[vid][i] = replace(c, status=status)

    def get_clip(self, clip_id: UUID) -> ClipRecord | None:
        return self.clips.get(clip_id)

    def get_clip_for_candidate(self, candidate_id: UUID) -> ClipRecord | None:
        return next((c for c in self.clips.values() if c.candidate_id == candidate_id), None)

    def create_clip(self, candidate: CandidateRecord, credit_text: str) -> ClipRecord:
        existing = self.get_clip_for_candidate(candidate.id)
        if existing is not None:
            return existing
        clip = ClipRecord(
            new_id(),
            candidate.id,
            candidate.video_id,
            candidate.start_s,
            candidate.end_s,
            "auto",
            None,
            None,
            None,
            "",
            credit_text,
        )
        self.clips[clip.id] = clip
        return clip

    def update_clip(self, clip_id: UUID, **fields: Any) -> ClipRecord:
        clip = replace(self.clips[clip_id], **fields)
        self.clips[clip_id] = clip
        return clip

    def get_caption_style(self, style_id: UUID | None) -> CaptionStyleRecord:
        if not self.styles:
            self.styles.append(CaptionStyleRecord(new_id(), "default", {"font": "DejaVu Sans"}))
        if style_id is None:
            return self.styles[0]
        for s in self.styles:
            if s.id == style_id:
                return s
        raise PipelineError("caption style not found")

    def source_crop_override(self, source_id: UUID | None) -> dict[str, Any] | None:
        return self.source_crops.get(source_id) if source_id else None

    def find_render(self, clip_id: UUID, kind: str, render_hash: str) -> RenderRecord | None:
        return self.renders.get((clip_id, kind, render_hash))

    def latest_render(self, clip_id: UUID, kind: str) -> RenderRecord | None:
        ok = [
            r
            for (cid, k, _), r in self.renders.items()
            if cid == clip_id and k == kind and r.status == "succeeded"
        ]
        return ok[-1] if ok else None

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
    ) -> RenderRecord:
        prev = self.renders.get((clip_id, kind, render_hash))
        r = RenderRecord(
            prev.id if prev else new_id(),
            clip_id,
            kind,
            render_hash,
            blob_key,
            status,
            duration_s,
            render_seconds,
        )
        self.renders.pop((clip_id, kind, render_hash), None)
        self.renders[(clip_id, kind, render_hash)] = r
        return r

    def record_feedback(
        self, kind: str, candidate_id: UUID | None, clip_id: UUID | None, **payload: Any
    ) -> None:
        self.feedback.append(
            {"kind": kind, "candidate_id": candidate_id, "clip_id": clip_id, **payload}
        )


class FakeSectionFetcher:
    """Writes a placeholder file; records requests. ``fail`` simulates a fetch error."""

    def __init__(self, fail: bool = False, align_s: float = 0.0) -> None:
        self.fail = fail
        self.align_s = align_s  # simulate HLS segment alignment (file starts earlier)
        self.calls: list[tuple[float, float, int]] = []

    def fetch_section(
        self, video: ResolvedVideo, start_s: float, end_s: float, workdir: Path, max_height: int
    ) -> SectionFile:
        self.calls.append((start_s, end_s, max_height))
        if self.fail:
            raise FetchFailed("fake: section download failed")
        p = workdir / "section.mp4"
        p.write_bytes(b"fake-video")
        start = max(0.0, start_s - self.align_s)
        return SectionFile(p, start, end_s - start)


class FakeFacecamDetector:
    def __init__(self, faces: FrameFaces | None = None) -> None:
        self.faces = faces or FrameFaces(1920, 1080, [[] for _ in range(8)])
        self.calls = 0

    def analyze(self, video: Path, times_s: Sequence[float]) -> FrameFaces:
        self.calls += 1
        return self.faces


class FakeRenderer:
    """Records specs and writes a placeholder MP4. ``fail`` simulates an ffmpeg crash."""

    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.specs: list[RenderSpec] = []

    @property
    def version(self) -> str:
        return "fake-renderer-1"

    def render(self, spec: RenderSpec) -> RenderResult:
        self.specs.append(spec)
        if self.fail:
            raise RenderFailed("fake: ffmpeg crashed")
        spec.output.write_bytes(b"fake-mp4:" + spec.quality.encode())
        return RenderResult(spec.duration_s, 0.5)
