"""AnalyzePipeline with fakes: degradation, isolation, idempotency, harness mode, takedown."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from clipforge.adapters.blobstore.local import LocalBlobStore
from clipforge.adapters.eventbus.in_process import InProcessEventBus
from clipforge.adapters.fakes.pipeline import (
    FakeAudioAnalyzer,
    FakeChatProvider,
    InMemoryAnalysisStore,
)
from clipforge.domain.ids import new_id
from clipforge.domain.values import BlobKey, ClipPolicy, Platform, WorkspaceId
from clipforge.ports.analysis import ScoringProfileRecord, SignalContext, SignalSeries
from clipforge.ports.pipeline import AssetRecord, PipelineError, ResolvedVideo, Transcript
from clipforge.scoring.evaluate import evaluate_at_k
from clipforge.services.analyze import AnalyzePipeline
from clipforge.services.ingest import SourceNotAllowed
from clipforge.signals import default_signals
from tests.scenario import make_scenario

WS = WorkspaceId(UUID("01936f00-0000-7000-8000-000000000001"))
SC = make_scenario()
PROFILE = ScoringProfileRecord(
    new_id(),
    "default",
    1,
    {"chat_velocity": 0.3, "emote_burst": 0.1, "audio_energy": 0.2, "speech_intensity": 0.15},
    {"max_clips_per_hour": 12},
)


class Boom:
    name, version, required = "boom", "1", False

    def extract(self, ctx: SignalContext) -> SignalSeries | None:
        raise RuntimeError("signal bug")


class Rig:
    def __init__(
        self,
        tmp: Path,
        *,
        chat_fail: bool = False,
        audio_fail: bool = False,
        extra_signals: list[object] | None = None,
        with_audio_blob: bool = True,
    ) -> None:
        self.store = InMemoryAnalysisStore(WS, profiles=[PROFILE])
        self.blobs = LocalBlobStore(tmp / "blobs")
        rv = ResolvedVideo(
            Platform.TWITCH,
            "https://www.twitch.tv/videos/42",
            "42",
            "streamer",
            None,
            "t",
            float(SC.n),
        )
        self.video, _ = self.store.upsert_video(rv, None, False)
        self.store.save_transcript(
            self.video.id, "audiohash", "cfg", Transcript("en", SC.n, "fake", tuple(SC.words))
        )
        if with_audio_blob:
            f = tmp / "a.m4a"
            f.write_bytes(b"x")
            key = BlobKey.for_workspace(WS, "videos", str(self.video.id), "audio.m4a")
            self.blobs.put(key, f)
            self.store.save_asset(self.video.id, "audio", AssetRecord(key, "audiohash", 1))
        self.chat = FakeChatProvider(Platform.TWITCH, SC.chat, fail=chat_fail)
        self.audio = FakeAudioAnalyzer(SC.rms_db.tolist(), fail=audio_fail)
        signals = [*default_signals(), *(extra_signals or [])]
        self.p = AnalyzePipeline(
            self.store,
            signals,
            {Platform.TWITCH: self.chat},
            self.audio,  # type: ignore[arg-type]
            self.blobs,
            InMemoryEventBus(),
        )

    def top_recall(self, res: object, k: int = 3) -> float:
        ranked = [(c.start_s, c.end_s) for c in res.candidates]  # type: ignore[attr-defined]
        return evaluate_at_k(ranked, SC.moments, k, tolerance_s=0).recall


class InMemoryEventBus(InProcessEventBus):
    pass


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def test_full_analysis_finds_moments_and_stores_candidates(rig: Rig) -> None:
    res = rig.p.analyze(rig.video.id)
    assert res.chat_status.startswith("fetched")
    assert set(res.signals.values()) == {"computed"}
    assert rig.top_recall(res) == 1.0
    stored = rig.store.list_candidates(rig.video.id)
    assert len(stored) == len(res.candidates) > 0
    assert rig.store.find_asset(rig.video.id, "chat_dump") is not None  # chat cached
    assert rig.store.jobs[(rig.video.id, "signals")]["status"].value == "succeeded"
    assert rig.store.jobs[(rig.video.id, "candidates")]["status"].value == "succeeded"


def test_rerun_uses_caches_and_does_not_duplicate(rig: Rig) -> None:
    rig.p.analyze(rig.video.id)
    n_signals, n_cands = len(rig.store.signal_rows), len(rig.store.list_candidates(rig.video.id))
    res = rig.p.analyze(rig.video.id)
    assert set(res.signals.values()) == {"cached"}
    assert res.chat_status.startswith("cached")
    assert (rig.chat.calls, rig.audio.calls) == (1, 1)  # chat and loudness fetched once
    assert len(rig.store.signal_rows) == n_signals
    assert len(rig.store.list_candidates(rig.video.id)) == n_cands


def test_reviewed_candidates_survive_reanalysis(rig: Rig) -> None:
    rig.p.analyze(rig.video.id)
    first = rig.store.candidates[rig.video.id][0]
    from dataclasses import replace

    rig.store.candidates[rig.video.id][0] = replace(first, status="approved")
    rig.p.analyze(rig.video.id, param_overrides={"setup_s": 15})  # different windows
    statuses = [c.status for c in rig.store.candidates[rig.video.id]]
    assert statuses.count("approved") == 1


def test_chat_down_degrades_to_audio_and_transcript(tmp_path: Path) -> None:
    rig = Rig(tmp_path, chat_fail=True)
    res = rig.p.analyze(rig.video.id)
    assert res.chat_status.startswith("unavailable")
    assert res.signals["chat_velocity"] == "unavailable"
    assert "chat_velocity" not in res.weights_used
    assert rig.top_recall(res) == 1.0  # still finds the moments


def test_audio_decode_failure_degrades(tmp_path: Path) -> None:
    rig = Rig(tmp_path, audio_fail=True)
    res = rig.p.analyze(rig.video.id)
    assert res.signals["audio_energy"] == "unavailable"
    assert res.candidates


def test_missing_audio_blob_degrades(tmp_path: Path) -> None:
    rig = Rig(tmp_path, with_audio_blob=False)
    res = rig.p.analyze(rig.video.id)
    assert res.signals["audio_energy"] == "unavailable"


def test_a_crashing_optional_signal_is_isolated(tmp_path: Path) -> None:
    rig = Rig(tmp_path, extra_signals=[Boom()])
    res = rig.p.analyze(rig.video.id)
    assert res.signals["boom"] == "failed: RuntimeError"
    assert rig.top_recall(res) == 1.0


def test_harness_mode_writes_no_candidates(rig: Rig) -> None:
    res = rig.p.analyze(rig.video.id, persist=False, weight_overrides={"audio_energy": 0.0})
    assert "audio_energy" not in res.weights_used
    assert rig.store.list_candidates(rig.video.id) == []
    assert (rig.video.id, "candidates") not in rig.store.jobs


def test_requires_transcript(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    rig.store.transcripts.clear()
    with pytest.raises(PipelineError, match="no transcript"):
        rig.p.analyze(rig.video.id)


def test_takedown_blocks_analysis(rig: Rig) -> None:
    src = rig.store.add_source(Platform.TWITCH, "streamer", ClipPolicy.OPEN_POLICY)
    rig.store.retire(src.id)
    with pytest.raises(SourceNotAllowed):
        rig.p.analyze(rig.video.id)
