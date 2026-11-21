"""M2 against Postgres: SQL analysis store, idempotency, API candidates, replay harness."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from apps.api.deps import container_dep, db_session
from apps.api.main import create_app
from clipforge.adapters.blobstore.local import LocalBlobStore
from clipforge.adapters.eventbus.in_process import InProcessEventBus
from clipforge.adapters.fakes.infra import FakeClock, FakeJobQueue
from clipforge.adapters.fakes.pipeline import (
    FakeAudioAnalyzer,
    FakeChatProvider,
    FakeFetcher,
    FakeResolver,
    FakeTranscriber,
)
from clipforge.adapters.misc import AllowAllQuota
from clipforge.domain.urls import parse_video_url
from clipforge.domain.values import BlobKey, Platform, WorkspaceId
from clipforge.infra.config import get_settings
from clipforge.infra.db import models as m
from clipforge.infra.db.pipeline_store import SqlPipelineStore
from clipforge.infra.registry import Container
from clipforge.infra.seed import seed
from clipforge.ports.pipeline import AssetRecord, ResolvedVideo, Transcript
from clipforge.scoring.evaluate import evaluate_at_k
from tests.scenario import make_scenario

pytestmark = pytest.mark.db

WS = WorkspaceId(UUID(get_settings().default_workspace_id))
SC = make_scenario()
LABELS = Path(__file__).parent.parent / "fixtures" / "labels.json"
AUTH = {"Authorization": "Bearer test-token"}


def count(db: Session, model: type[m.Base]) -> int:
    return int(db.scalar(select(func.count()).select_from(model)) or 0)


@pytest.fixture
def container(tmp_path: Path) -> Container:
    return Container(
        settings=get_settings(),
        clock=FakeClock(),
        blobs=LocalBlobStore(tmp_path / "blobs"),
        events=InProcessEventBus(),
        quota=AllowAllQuota(),
        resolvers=[FakeResolver(Platform.TWITCH)],
        fetcher=FakeFetcher(),
        transcriber=FakeTranscriber(),
        audio=FakeAudioAnalyzer(SC.rms_db.tolist()),
        chat_overrides={Platform.TWITCH: FakeChatProvider(Platform.TWITCH, SC.chat)},
        _queue=FakeJobQueue(),
    )


@pytest.fixture
def video_id(db: Session, container: Container, tmp_path: Path) -> UUID:
    """A transcribed synthetic video, as M1 would leave it."""
    seed(db, get_settings())
    store = SqlPipelineStore(db, WS)
    rv = ResolvedVideo(
        Platform.TWITCH,
        "https://www.twitch.tv/videos/42",
        "42",
        "streamer",
        None,
        "synthetic",
        float(SC.n),
    )
    v, _ = store.upsert_video(rv, None, True)
    f = tmp_path / "a.m4a"
    f.write_bytes(b"x")
    key = BlobKey.for_workspace(WS, "videos", str(v.id), "audio.m4a")
    container.blobs.put(key, f)
    store.save_asset(v.id, "audio", AssetRecord(key, "audiohash", 1, float(SC.n)))
    store.save_transcript(v.id, "audiohash", "cfg", Transcript("en", SC.n, "f", tuple(SC.words)))
    return v.id


def test_analyze_persists_signals_and_candidates_idempotently(
    db: Session, container: Container, video_id: UUID
) -> None:
    p = container.analyze_pipeline(db, WS)
    res = p.analyze(video_id)
    ranked = [(c.start_s, c.end_s) for c in res.candidates]
    assert evaluate_at_k(ranked, SC.moments, 3, tolerance_s=0).recall == 1.0

    names = set(db.scalars(select(m.SignalSeries.signal_name)))
    assert names == {
        "_audio_rms_db",
        "chat_velocity",
        "emote_burst",
        "audio_energy",
        "speech_intensity",
    }
    chat_assets = db.scalars(select(m.VideoAsset).where(m.VideoAsset.kind == "chat_dump")).all()
    assert len(chat_assets) == 1
    snap = (count(db, m.SignalSeries), count(db, m.Candidate), count(db, m.VideoAsset))

    again = container.analyze_pipeline(db, WS).analyze(video_id)
    assert set(again.signals.values()) == {"cached"}
    assert (count(db, m.SignalSeries), count(db, m.Candidate), count(db, m.VideoAsset)) == snap

    cand = db.scalars(select(m.Candidate).order_by(m.Candidate.score.desc())).first()
    assert cand is not None and cand.rank == 1 and "peak_s" in cand.components
    assert cand.reason


def test_reanalysis_keeps_reviewed_candidates(
    db: Session, container: Container, video_id: UUID
) -> None:
    container.analyze_pipeline(db, WS).analyze(video_id)
    top = db.scalars(select(m.Candidate).order_by(m.Candidate.score.desc())).first()
    assert top is not None
    top.status = "approved"
    db.flush()
    container.analyze_pipeline(db, WS).analyze(video_id, param_overrides={"setup_s": 12})
    approved = db.scalars(select(m.Candidate).where(m.Candidate.status == "approved")).all()
    assert [c.id for c in approved] == [top.id]


def test_api_lists_ranked_candidates(db: Session, container: Container, video_id: UUID) -> None:
    container.analyze_pipeline(db, WS).analyze(video_id)
    app = create_app()

    def _session() -> Iterator[Session]:
        yield db

    app.dependency_overrides[db_session] = _session
    app.dependency_overrides[container_dep] = lambda: container
    client = TestClient(app)
    r = client.get(f"/api/v1/videos/{video_id}/candidates", headers=AUTH)
    assert r.status_code == 200
    rows = r.json()
    assert rows and rows == sorted(rows, key=lambda c: -c["score"])
    assert {"chat_velocity", "audio_energy"} <= set(rows[0]["components"])
    assert client.get(f"/api/v1/videos/{UUID(int=1)}/candidates", headers=AUTH).status_code == 404


def test_replay_harness_on_labels_fixture(
    db: Session, container: Container, video_id: UUID
) -> None:
    """What `clipforge eval` does: map labelled URLs to videos, score without persisting."""
    spec = json.loads(LABELS.read_text(encoding="utf-8"))
    store = SqlPipelineStore(db, WS)
    item = spec["videos"][0]
    parsed = parse_video_url(item["url"])
    assert parsed is not None
    video = store.video_by_external(parsed.platform, parsed.external_id)
    assert video is not None and video.id == video_id
    res = container.analyze_pipeline(db, WS).analyze(video.id, persist=False)
    ranked = [(c.start_s, c.end_s) for c in res.candidates]
    r3 = evaluate_at_k(ranked, item["good_moments_s"], 3, item["tolerance_s"])
    assert (r3.precision, r3.recall) == (1.0, 1.0)
    assert count(db, m.Candidate) == 0  # harness never writes candidates


# ---------------------------------------------------------------- M3: judge against Postgres
def test_judge_persists_verdicts_and_api_shows_them(
    db: Session, container: Container, video_id: UUID
) -> None:
    from clipforge.adapters.fakes.pipeline import FakeJudge

    container.judge_override = FakeJudge(
        lambda it: 0.95 if it.start_s <= 1500 <= it.end_s else 0.05
    )
    prof = db.scalars(select(m.ScoringProfile)).first()
    assert prof is not None
    res = container.analyze_pipeline(db, WS).analyze(
        video_id, weight_overrides={"llm_moment_score": 3.0}
    )
    assert res.judge_status.startswith("ok:")

    rows = db.scalars(select(m.Candidate).order_by(m.Candidate.rank)).all()
    assert [c.rank for c in rows] == list(range(1, len(rows) + 1))
    assert rows[0].start_s <= 1500 <= rows[0].end_s
    assert rows[0].title_suggestions and rows[0].hook_text and rows[0].judge
    usage = set(db.scalars(select(m.UsageEvent.kind)))
    assert {"llm_tokens_in", "llm_tokens_out", "llm_cost_usd_est"} <= usage

    app = create_app()

    def _session() -> Iterator[Session]:
        yield db

    app.dependency_overrides[db_session] = _session
    app.dependency_overrides[container_dep] = lambda: container
    body = TestClient(app).get(f"/api/v1/videos/{video_id}/candidates", headers=AUTH).json()
    assert body[0]["title_suggestions"] and body[0]["hook_text"]
    assert body[0]["judge"]["model"] == "fake-judge"
    assert "llm_moment_score" in body[0]["components"]
