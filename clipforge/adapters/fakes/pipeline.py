"""Fakes for pipeline ports. Deterministic, no network, with failure injection."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

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
from clipforge.ports.pipeline import (
    AssetRecord,
    FetchedFile,
    FetchFailed,
    ResolvedVideo,
    ResolveFailed,
    SourceRecord,
    TranscribeFailed,
    Transcript,
    UnsupportedUrl,
    VideoRecord,
    Word,
)


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
                nv = VideoRecord(
                    v.id,
                    v.workspace_id,
                    source_id,
                    v.platform,
                    rv.url,
                    v.external_id,
                    v.status,
                    rv.duration_s,
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
        )
        self.videos[v.id] = v
        return v, True

    def get_video(self, video_id: UUID) -> VideoRecord | None:
        return self.videos.get(video_id)

    def set_video_status(self, video_id: UUID, status: str) -> None:
        v = self.videos[video_id]
        self.videos[video_id] = VideoRecord(
            v.id,
            v.workspace_id,
            v.source_id,
            v.platform,
            v.url,
            v.external_id,
            status,
            v.duration_s,
        )

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
