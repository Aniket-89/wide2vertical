"""M1 use cases: ingest (resolve + allowlist) → fetch audio → transcribe.

Every stage is idempotent: a stage whose output already exists for the same input hash is
recorded as SKIPPED and returns the cached result.
"""

from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID

from clipforge.compliance.allowlist import SourceFacts, Verdict, decide
from clipforge.domain.events import StageCompleted, StageFailed, VideoIngested
from clipforge.domain.values import BlobKey, JobStatus, SourceStatus, Stage
from clipforge.ports.infra import BlobStore, EventBus, QuotaService
from clipforge.ports.pipeline import (
    MIN_COMPLETENESS,
    AssetRecord,
    FetchFailed,
    MediaFetcher,
    PipelineError,
    PipelineStore,
    SourceResolver,
    Transcriber,
    UnsupportedUrl,
    VideoRecord,
)


class SourceNotAllowed(PipelineError):
    """The allowlist refused this URL. ``reason`` is user-facing."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class QuotaExceeded(PipelineError):
    pass


@dataclass(frozen=True, slots=True)
class IngestResult:
    video: VideoRecord
    verdict: Verdict
    reason: str
    created: bool


def _complete(measured: float | None, expected: float | None) -> bool:
    """Is a cached asset complete? A *measured* short asset is not, so it gets re-fetched.
    Unknown duration (no ffprobe on this host) is trusted; otherwise every run would
    re-download."""
    if not expected or measured is None:
        return True
    return measured >= MIN_COMPLETENESS * expected


def sha256_text(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


class IngestPipeline:
    def __init__(
        self,
        store: PipelineStore,
        resolvers: Sequence[SourceResolver],
        fetcher: MediaFetcher,
        transcriber: Transcriber,
        blobs: BlobStore,
        events: EventBus,
        quota: QuotaService,
    ) -> None:
        self.store = store
        self.resolvers = resolvers
        self.fetcher = fetcher
        self.transcriber = transcriber
        self.blobs = blobs
        self.events = events
        self.quota = quota

    # ------------------------------------------------------------ helpers
    @contextmanager
    def _stage(self, video_id: UUID, stage: Stage, input_hash: str) -> Iterator[list[bool]]:
        """Track a stage in job_runs. Yield a 1-item list; set [0]=True to mark it SKIPPED."""
        run_id = self.store.begin_stage(video_id, stage, input_hash)
        skipped = [False]
        try:
            yield skipped
        except Exception as exc:
            self.store.end_stage(run_id, JobStatus.FAILED, f"{type(exc).__name__}: {exc}"[:2000])
            self.store.set_video_status(video_id, f"{stage.value}_failed")
            self.events.publish(
                StageFailed(
                    workspace_id=self.store.workspace_id,
                    video_id=video_id,
                    stage=stage,
                    error=type(exc).__name__,
                )
            )
            raise
        self.store.end_stage(run_id, JobStatus.SKIPPED if skipped[0] else JobStatus.SUCCEEDED)
        self.store.set_video_status(video_id, f"{stage.value}_done")
        self.events.publish(
            StageCompleted(workspace_id=self.store.workspace_id, video_id=video_id, stage=stage)
        )

    def _resolver_for(self, url: str) -> SourceResolver:
        for r in self.resolvers:
            if r.supports(url):
                return r
        raise UnsupportedUrl(f"no resolver supports {url!r}")

    def _require_video(self, video_id: UUID) -> VideoRecord:
        video = self.store.get_video(video_id)
        if video is None:
            raise PipelineError(f"video {video_id} not found in this workspace")
        return video

    def _require_not_retired(self, video: VideoRecord, stage: Stage) -> None:
        """Takedowns apply to work already queued: re-check before every heavy stage."""
        src = self.store.source_for_video(video)
        if src is not None and src.status is SourceStatus.RETIRED:
            self.store.audit(
                "pipeline.blocked", "video", video.id, stage=stage.value, reason="source retired"
            )
            self.store.set_video_status(video.id, "source_retired")
            raise SourceNotAllowed("source is retired (takedown); processing stopped")

    # ------------------------------------------------------------ stage 1: ingest
    def ingest(self, url: str, *, override: bool = False) -> IngestResult:
        rv = self._resolver_for(url).resolve(url)
        source = self.store.find_source(rv.platform, rv.channel_handle, rv.channel_id)
        facts = (
            SourceFacts(source.clip_policy, source.status, bool(source.required_credit_text))
            if source
            else None
        )
        decision = decide(facts, override=override)

        if decision.verdict is Verdict.REFUSE:
            self.store.audit(
                "ingest.refused",
                "url",
                None,
                url=url,
                platform=rv.platform.value,
                channel=rv.channel_handle,
                reason=decision.reason,
            )
            raise SourceNotAllowed(decision.reason)

        if rv.is_live:
            raise PipelineError("live streams are not supported until M7; use the VOD URL")

        override_used = decision.verdict is Verdict.ALLOW_WITH_OVERRIDE
        video, created = self.store.upsert_video(rv, source.id if source else None, override_used)
        if override_used:
            self.store.audit(
                "ingest.override",
                "video",
                video.id,
                url=url,
                channel=rv.channel_handle,
                reason=decision.reason,
            )
        with self._stage(video.id, Stage.INGEST, sha256_text(rv.platform.value, rv.external_id)):
            pass
        self.events.publish(
            VideoIngested(
                workspace_id=self.store.workspace_id,
                video_id=video.id,
                override_used=override_used,
            )
        )
        return IngestResult(video, decision.verdict, decision.reason, created=created)

    # ------------------------------------------------------------ stage 2: fetch audio
    def fetch_audio(self, video_id: UUID) -> AssetRecord:
        video = self._require_video(video_id)
        self._require_not_retired(video, Stage.FETCH)
        with self._stage(video.id, Stage.FETCH, sha256_text(video.url, "audio")) as skip:
            cached = self.store.find_asset(video.id, "audio")
            if (
                cached is not None
                and self.blobs.exists(cached.blob_key)
                and _complete(cached.duration_s, video.duration_s)
            ):
                skip[0] = True
                return cached

            rv = self._resolver_for(video.url).resolve(video.url)
            with tempfile.TemporaryDirectory(prefix="clipforge-fetch-") as tmp:
                f = self.fetcher.fetch_audio(rv, Path(tmp))
                expected = rv.duration_s
                if (
                    f.duration_s is not None
                    and expected
                    and (f.duration_s < MIN_COMPLETENESS * expected)
                ):
                    raise FetchFailed(f"truncated audio: {f.duration_s:.0f}s of {expected:.0f}s")
                key = BlobKey.for_workspace(
                    self.store.workspace_id, "videos", str(video.id), f"audio.{f.ext}"
                )
                written = self.blobs.put(key, f.path)
            asset = AssetRecord(key, f.content_hash, written, duration_s=f.duration_s)
            self.store.save_asset(video.id, "audio", asset)
            self.store.record_usage("storage_bytes", written, "bytes", video.id, asset="audio")
            return asset

    # ------------------------------------------------------------ stage 3: transcribe
    def transcribe(self, video_id: UUID) -> bool:
        """Returns True if a new transcript was produced, False if the cache was reused."""
        video = self._require_video(video_id)
        self._require_not_retired(video, Stage.TRANSCRIBE)
        audio = self.store.find_asset(video.id, "audio")
        if audio is None:
            raise PipelineError("no audio asset; run the fetch stage first")
        cfg = self.transcriber.config_version
        input_hash = sha256_text(audio.content_hash, cfg)

        with self._stage(video.id, Stage.TRANSCRIBE, input_hash) as skip:
            if self.store.has_transcript(video.id, audio.content_hash, cfg):
                skip[0] = True
                return False
            minutes = (video.duration_s or 0) / 60
            q = self.quota.check(self.store.workspace_id, "transcribe_minutes", minutes)
            if not q.allowed:
                raise QuotaExceeded(q.reason or "transcription quota exceeded")
            with self.blobs.open_local(audio.blob_key) as path:
                t = self.transcriber.transcribe(path)
            self.store.save_transcript(video.id, audio.content_hash, cfg, t)
            self.store.record_usage(
                "transcribe_minutes",
                round(t.duration_s / 60, 4),
                "minutes",
                video.id,
                model=t.model,
            )
            return True

    # ------------------------------------------------------------ convenience
    def run_all(self, url: str, *, override: bool = False) -> IngestResult:
        res = self.ingest(url, override=override)
        self.fetch_audio(res.video.id)
        self.transcribe(res.video.id)
        return res
