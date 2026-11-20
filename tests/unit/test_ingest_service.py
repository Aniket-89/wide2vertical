"""IngestPipeline with fakes: allowlist, idempotency, failure injection, metering."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from clipforge.adapters.blobstore.local import LocalBlobStore
from clipforge.adapters.eventbus.in_process import InProcessEventBus
from clipforge.adapters.fakes.pipeline import (
    FakeFetcher,
    FakeResolver,
    FakeTranscriber,
    InMemoryPipelineStore,
)
from clipforge.adapters.misc import AllowAllQuota
from clipforge.domain.events import DomainEvent, StageFailed
from clipforge.domain.values import ClipPolicy, JobStatus, Platform, SourceStatus, WorkspaceId
from clipforge.ports.infra import QuotaDecision
from clipforge.ports.pipeline import (
    FetchFailed,
    PipelineError,
    ResolveFailed,
    TranscribeFailed,
    UnsupportedUrl,
)
from clipforge.services.ingest import IngestPipeline, QuotaExceeded, SourceNotAllowed

WS = WorkspaceId(UUID("01936f00-0000-7000-8000-000000000001"))
YT = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
TW = "https://www.twitch.tv/videos/2245123456"


class DenyQuota:
    def check(self, workspace_id: WorkspaceId, kind: str, quantity: float) -> QuotaDecision:
        return QuotaDecision(False, "monthly minutes used up")


class Rig:
    def __init__(self, tmp: Path, **kw: object) -> None:
        self.store = InMemoryPipelineStore(WS)
        self.yt = FakeResolver(Platform.YOUTUBE)
        self.tw = FakeResolver(Platform.TWITCH, fail=bool(kw.get("resolver_down")))
        self.fetcher = FakeFetcher(fail=bool(kw.get("fetch_fail")))
        self.transcriber = FakeTranscriber(fail=kw.get("transcribe_fail"))  # type: ignore[arg-type]
        self.events: list[DomainEvent] = []
        bus = InProcessEventBus()
        bus.subscribe(DomainEvent, self.events.append)
        self.blobs = LocalBlobStore(tmp / "blobs")
        self.p = IngestPipeline(
            self.store,
            [self.yt, self.tw],
            self.fetcher,
            self.transcriber,
            self.blobs,
            bus,
            kw.get("quota", AllowAllQuota()),  # type: ignore[arg-type]
        )

    def allow(self, platform: Platform = Platform.YOUTUBE) -> None:
        self.store.add_source(platform, "streamer", ClipPolicy.OPEN_POLICY)

    def stage_status(self, video_id: UUID, stage: str) -> JobStatus:
        return self.store.jobs[(video_id, stage)]["status"]  # type: ignore[no-any-return]


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


# ---------------------------------------------------------------- allowlist
def test_refuses_unlisted_channel_and_audits(rig: Rig) -> None:
    with pytest.raises(SourceNotAllowed, match="not in allowlist"):
        rig.p.ingest(YT)
    assert rig.store.videos == {}
    assert rig.store.audits[-1]["action"] == "ingest.refused"
    assert rig.fetcher.calls == 0


def test_override_processes_and_audits(rig: Rig) -> None:
    res = rig.p.ingest(YT, override=True)
    assert res.verdict.value == "allow_with_override"
    assert [a["action"] for a in rig.store.audits] == ["ingest.override"]
    assert res.video.source_id is None


def test_retired_source_refused_even_with_override(rig: Rig) -> None:
    rig.store.add_source(
        Platform.YOUTUBE, "@Streamer", ClipPolicy.OWN_CHANNEL, status=SourceStatus.RETIRED
    )
    with pytest.raises(SourceNotAllowed, match="retired"):
        rig.p.ingest(YT, override=True)


def test_unsupported_url(rig: Rig) -> None:
    with pytest.raises(UnsupportedUrl):
        rig.p.ingest("https://example.com/video/1")


# ---------------------------------------------------------------- happy path + idempotency
def test_run_all_produces_transcript_and_usage(rig: Rig) -> None:
    rig.allow()
    res = rig.p.run_all(YT)
    vid = res.video.id
    assert res.created
    assert rig.store.find_asset(vid, "audio") is not None
    assert len(rig.store.transcripts) == 1
    for stage in ("ingest", "fetch", "transcribe"):
        assert rig.stage_status(vid, stage) is JobStatus.SUCCEEDED
    kinds = {u["kind"] for u in rig.store.usage}
    assert kinds == {"storage_bytes", "transcribe_minutes"}
    assert rig.store.videos[vid].status == "transcribe_done"


def test_rerun_is_idempotent_and_uses_cache(rig: Rig) -> None:
    rig.allow()
    first = rig.p.run_all(YT)
    second = rig.p.run_all(YT)
    assert first.video.id == second.video.id
    assert not second.created
    assert len(rig.store.videos) == 1
    assert len(rig.store.transcripts) == 1
    assert rig.fetcher.calls == 1 and rig.transcriber.calls == 1
    vid = first.video.id
    assert rig.stage_status(vid, "fetch") is JobStatus.SKIPPED
    assert rig.stage_status(vid, "transcribe") is JobStatus.SKIPPED
    assert len([u for u in rig.store.usage if u["kind"] == "transcribe_minutes"]) == 1


def test_new_transcriber_config_invalidates_cache(rig: Rig) -> None:
    rig.allow()
    vid = rig.p.run_all(YT).video.id
    rig.transcriber.version = "fake-2"
    assert rig.p.transcribe(vid) is True
    assert len(rig.store.transcripts) == 2


def test_missing_blob_triggers_refetch(rig: Rig) -> None:
    rig.allow()
    vid = rig.p.run_all(YT).video.id
    asset = rig.store.find_asset(vid, "audio")
    assert asset is not None
    rig.blobs.delete(asset.blob_key)  # e.g. retention cleanup
    rig.p.fetch_audio(vid)
    assert rig.fetcher.calls == 2


# ---------------------------------------------------------------- failure injection
def test_fetch_failure_recorded_and_isolated(tmp_path: Path) -> None:
    rig = Rig(tmp_path, fetch_fail=True)
    rig.allow()
    vid = rig.p.ingest(YT).video.id
    with pytest.raises(FetchFailed):
        rig.p.fetch_audio(vid)
    assert rig.stage_status(vid, "fetch") is JobStatus.FAILED
    assert rig.store.videos[vid].status == "fetch_failed"
    assert any(isinstance(e, StageFailed) for e in rig.events)
    # retry after the outage: a second attempt is recorded
    rig.fetcher.fail = False
    rig.p.fetch_audio(vid)
    assert rig.store.jobs[(vid, "fetch")]["attempts"] == 2


def test_whisper_oom_recorded(tmp_path: Path) -> None:
    rig = Rig(tmp_path, transcribe_fail=TranscribeFailed("CUDA out of memory"))
    rig.allow()
    vid = rig.p.ingest(YT).video.id
    rig.p.fetch_audio(vid)
    with pytest.raises(TranscribeFailed):
        rig.p.transcribe(vid)
    run = next(r for r in rig.store.runs.values() if r["job"] == (vid, "transcribe"))
    assert "out of memory" in run["error"]
    assert rig.store.transcripts == {}


def test_resolver_down(tmp_path: Path) -> None:
    rig = Rig(tmp_path, resolver_down=True)
    with pytest.raises(ResolveFailed):
        rig.p.ingest(TW)


def test_quota_denial(tmp_path: Path) -> None:
    rig = Rig(tmp_path, quota=DenyQuota())
    rig.allow()
    vid = rig.p.ingest(YT).video.id
    rig.p.fetch_audio(vid)
    with pytest.raises(QuotaExceeded):
        rig.p.transcribe(vid)


def test_transcribe_requires_audio(rig: Rig) -> None:
    rig.allow()
    vid = rig.p.ingest(YT).video.id
    with pytest.raises(PipelineError, match="no audio"):
        rig.p.transcribe(vid)


def test_truncated_fetch_is_rejected_not_stored(rig: Rig) -> None:
    rig.allow()
    vid = rig.p.ingest(YT).video.id  # fake resolver reports 60 s
    rig.fetcher.duration_s = 20.0  # ffmpeg quietly stopped early
    with pytest.raises(FetchFailed, match="truncated"):
        rig.p.fetch_audio(vid)
    assert rig.store.find_asset(vid, "audio") is None
    assert rig.stage_status(vid, "fetch") is JobStatus.FAILED

    rig.fetcher.duration_s = 59.9  # within tolerance
    rig.p.fetch_audio(vid)
    assert rig.store.find_asset(vid, "audio") is not None


def test_takedown_between_ingest_and_fetch_stops_processing(rig: Rig) -> None:
    src = rig.store.add_source(Platform.YOUTUBE, "streamer", ClipPolicy.OPEN_POLICY)
    vid = rig.p.ingest(YT).video.id
    rig.store.retire(src.id)  # creator asks for takedown while the fetch job is queued
    with pytest.raises(SourceNotAllowed, match="retired"):
        rig.p.fetch_audio(vid)
    assert rig.fetcher.calls == 0
    assert rig.store.videos[vid].status == "source_retired"
    assert rig.store.audits[-1]["action"] == "pipeline.blocked"


def test_takedown_between_fetch_and_transcribe_stops_processing(rig: Rig) -> None:
    src = rig.store.add_source(Platform.YOUTUBE, "streamer", ClipPolicy.OPEN_POLICY)
    vid = rig.p.ingest(YT).video.id
    rig.p.fetch_audio(vid)
    rig.store.retire(src.id)
    with pytest.raises(SourceNotAllowed):
        rig.p.transcribe(vid)
    assert rig.transcriber.calls == 0


def test_override_video_blocked_once_channel_is_allowlisted_then_retired(rig: Rig) -> None:
    vid = rig.p.ingest(YT, override=True).video.id  # no source yet
    src = rig.store.add_source(Platform.YOUTUBE, "streamer", ClipPolicy.OPEN_POLICY)
    rig.store.retire(src.id)
    with pytest.raises(SourceNotAllowed):
        rig.p.fetch_audio(vid)  # matched via the video's stored channel


def test_measured_short_cached_asset_is_refetched(rig: Rig) -> None:
    rig.allow()
    rig.fetcher.duration_s = 60.0
    vid = rig.p.run_all(YT).video.id
    asset = rig.store.find_asset(vid, "audio")
    assert asset is not None
    from dataclasses import replace

    rig.store.assets[(vid, "audio")] = replace(asset, duration_s=10.0)  # legacy truncated asset
    rig.p.fetch_audio(vid)
    assert rig.fetcher.calls == 2
