"""Judge stage inside AnalyzePipeline, with FakeJudge: ranking, budget, cache, failures."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from clipforge.adapters.blobstore.local import LocalBlobStore
from clipforge.adapters.eventbus.in_process import InProcessEventBus
from clipforge.adapters.fakes.pipeline import (
    FakeAudioAnalyzer,
    FakeChatProvider,
    FakeJudge,
    InMemoryAnalysisStore,
)
from clipforge.adapters.judges.null_judge import NullJudge
from clipforge.domain.ids import new_id
from clipforge.domain.values import BlobKey, JobStatus, Platform, WorkspaceId
from clipforge.ports.analysis import ScoringProfileRecord
from clipforge.ports.judge import JudgeInput, JudgeUsage, LLMJudge
from clipforge.ports.pipeline import AssetRecord, ResolvedVideo, Transcript, Word
from clipforge.services.analyze import AnalyzePipeline
from clipforge.services.judging import JudgeConfig, blend, judge_weight, window_transcript
from clipforge.signals import default_signals
from tests.scenario import make_scenario

WS = WorkspaceId(UUID("01936f00-0000-7000-8000-000000000001"))
SC = make_scenario()
WEIGHTS = {
    "chat_velocity": 0.3,
    "emote_burst": 0.1,
    "audio_energy": 0.2,
    "speech_intensity": 0.15,
    "llm_moment_score": 0.2,
}


def make(
    tmp: Path,
    judge: LLMJudge | None,
    *,
    top_n: int = 30,
    batch: int = 8,
    weights: dict[str, float] | None = None,
) -> tuple[AnalyzePipeline, InMemoryAnalysisStore, UUID]:
    prof = ScoringProfileRecord(
        new_id(),
        "default",
        1,
        weights or WEIGHTS,
        {"max_clips_per_hour": 20, "judge_top_n_per_hour": top_n},
    )
    store = InMemoryAnalysisStore(WS, profiles=[prof])
    blobs = LocalBlobStore(tmp / "blobs")
    rv = ResolvedVideo(
        Platform.TWITCH, "https://www.twitch.tv/videos/42", "42", "streamer", None, "t", float(SC.n)
    )
    video, _ = store.upsert_video(rv, None, False)
    store.save_transcript(video.id, "h", "cfg", Transcript("en", SC.n, "fake", tuple(SC.words)))
    f = tmp / "a.m4a"
    f.write_bytes(b"x")
    key = BlobKey.for_workspace(WS, "videos", str(video.id), "audio.m4a")
    blobs.put(key, f)
    store.save_asset(video.id, "audio", AssetRecord(key, "h", 1))
    p = AnalyzePipeline(
        store,
        default_signals(),
        {Platform.TWITCH: FakeChatProvider(Platform.TWITCH, SC.chat)},
        FakeAudioAnalyzer(SC.rms_db.tolist()),
        blobs,
        InProcessEventBus(),
        judge=judge,
        judge_config=JudgeConfig(batch_size=batch),
    )
    return p, store, video.id


def favour_late(item: JudgeInput) -> float:
    """Judge loves the window around t=1500 s, dislikes everything else."""
    return 0.95 if item.start_s <= 1500 <= item.end_s else 0.05


# ---------------------------------------------------------------- pure helpers
def test_judge_weight_mirrors_signal_renormalisation() -> None:
    assert judge_weight(
        WEIGHTS, ["chat_velocity", "emote_burst", "audio_energy", "speech_intensity"]
    ) == pytest.approx(0.2 / 0.95)
    assert judge_weight(WEIGHTS, ["audio_energy", "speech_intensity"]) == pytest.approx(0.2 / 0.55)
    assert judge_weight({"audio_energy": 1.0}, ["audio_energy"]) == 0.0
    assert blend(0.4, 1.0, 0.25) == pytest.approx(0.55)


def test_window_transcript_is_scoped_and_capped() -> None:
    words = [Word("a", 1, 1.2, 1), Word("b", 5, 5.2, 1), Word("c" * 50, 6, 6.5, 1)]
    assert window_transcript(words, 0, 5, 100) == "a"
    assert window_transcript(words, 0, 10, 10).endswith("…")


# ---------------------------------------------------------------- pipeline
def test_judge_reranks_and_stores_titles(tmp_path: Path) -> None:
    judge = FakeJudge(favour_late)
    p, store, vid = make(tmp_path, judge, weights={**WEIGHTS, "llm_moment_score": 3.0})
    res = p.analyze(vid)
    assert res.judge_status.startswith("ok:")
    top = store.list_candidates(vid)[0]
    assert top.start_s <= 1500 <= top.end_s  # judge's favourite moved to #1
    assert top.rank == 1
    assert top.title_suggestions and top.hook_text.startswith("hook")
    assert top.judge is not None and top.judge["prompt_id"] == "judge.moment.v1"
    assert {"signal_score", "llm_moment_score"} <= set(top.components)
    assert top.reason.startswith("fake reason") and "signals:" in top.reason
    assert res.candidates[0].start_s == top.start_s  # returned ranking matches stored
    assert store.jobs[(vid, "judge")]["status"] is JobStatus.SUCCEEDED


def test_only_top_n_windows_are_judged_in_batches(tmp_path: Path) -> None:
    judge = FakeJudge()
    p, store, vid = make(tmp_path, judge, top_n=4, batch=3)  # 1800 s → ceil(0.5 h * 4) = 2
    p.analyze(vid)
    assert sum(len(b) for b in judge.batches) == 2
    judged = [c for c in store.list_candidates(vid) if c.judge]
    assert len(judged) == 2


def test_batching(tmp_path: Path) -> None:
    judge = FakeJudge()
    p, _, vid = make(tmp_path, judge, top_n=60, batch=3)  # 30 windows budget, fewer exist
    p.analyze(vid)
    assert all(len(b) <= 3 for b in judge.batches) and len(judge.batches) >= 2


def test_verdicts_are_cached_by_content(tmp_path: Path) -> None:
    judge = FakeJudge()
    p, _, vid = make(tmp_path, judge)
    first = p.analyze(vid)
    calls = judge.calls
    second = p.analyze(vid)  # candidates are rebuilt, but windows are identical
    assert judge.calls == calls
    assert "cached" in second.judge_status and second.judge_cost_usd == 0
    assert first.judge_cost_usd > 0


def test_partial_failure_keeps_other_batches(tmp_path: Path) -> None:
    judge = FakeJudge(fail_calls={1})
    p, store, vid = make(tmp_path, judge, batch=2)
    res = p.analyze(vid)
    assert res.judge_status.startswith("partial:")
    rows = store.list_candidates(vid)
    assert any(c.judge for c in rows) and any(not c.judge for c in rows)


def test_llm_timeout_degrades_to_signal_scores(tmp_path: Path) -> None:
    judge = FakeJudge(always_fail=True)
    p, store, vid = make(tmp_path, judge)
    baseline = make(tmp_path / "b", None)
    res = p.analyze(vid)
    ref = baseline[0].analyze(baseline[2])
    assert res.judge_status.startswith("unavailable: fake: LLM timeout")
    assert [c.score for c in res.candidates] == [c.score for c in ref.candidates]
    assert store.jobs[(vid, "judge")]["status"] is JobStatus.FAILED
    assert store.videos[vid].status == "judge_failed"
    assert all(c.judge is None for c in store.list_candidates(vid))


def test_null_judge_skips_stage(tmp_path: Path) -> None:
    p, store, vid = make(tmp_path, NullJudge())
    res = p.analyze(vid)
    assert res.judge_status == "disabled (NullJudge)"
    assert (vid, "judge") not in store.jobs


def test_usage_and_cost_are_metered(tmp_path: Path) -> None:
    judge = FakeJudge()
    p, store, vid = make(tmp_path, judge)
    res = p.analyze(vid)
    kinds = {u["kind"]: u["quantity"] for u in store.usage}
    assert kinds["llm_tokens_in"] > 0 and kinds["llm_tokens_out"] > 0
    assert kinds["llm_cost_usd_est"] == pytest.approx(res.judge_cost_usd)
    cfg = JudgeConfig()
    assert cfg.cost_usd(JudgeUsage(1_000_000, 1_000_000)) == pytest.approx(0.60)


def test_harness_skips_judge_unless_asked(tmp_path: Path) -> None:
    judge = FakeJudge(favour_late)
    p, store, vid = make(tmp_path, judge, weights={**WEIGHTS, "llm_moment_score": 3.0})
    plain = p.analyze(vid, persist=False)
    assert judge.calls == 0 and plain.judge_status == "disabled"
    judged = p.analyze(vid, persist=False, with_judge=True)
    assert judge.calls > 0
    assert judged.candidates[0].start_s <= 1500 <= judged.candidates[0].end_s
    assert store.list_candidates(vid) == []  # harness never writes candidates
