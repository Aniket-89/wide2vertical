"""M1 end-to-end against Postgres with fake external adapters: service + SQL store + API."""

from __future__ import annotations

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
from clipforge.adapters.fakes.pipeline import FakeFetcher, FakeResolver, FakeTranscriber
from clipforge.adapters.misc import AllowAllQuota
from clipforge.domain.values import ClipPolicy, Platform, WorkspaceId
from clipforge.infra.config import get_settings
from clipforge.infra.db import models as m
from clipforge.infra.db.sources import SourceAdmin
from clipforge.infra.registry import Container
from clipforge.infra.seed import seed
from clipforge.services.ingest import SourceNotAllowed

pytestmark = pytest.mark.db

WS = WorkspaceId(UUID(get_settings().default_workspace_id))
YT = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
TW = "https://www.twitch.tv/videos/2245123456"
AUTH = {"Authorization": "Bearer test-token"}


def count(db: Session, model: type[m.Base]) -> int:
    return int(db.scalar(select(func.count()).select_from(model)) or 0)


@pytest.fixture
def container(tmp_path: Path) -> Container:
    s = get_settings()
    return Container(
        settings=s,
        clock=FakeClock(),
        blobs=LocalBlobStore(tmp_path / "blobs"),
        events=InProcessEventBus(),
        quota=AllowAllQuota(),
        resolvers=[FakeResolver(Platform.YOUTUBE), FakeResolver(Platform.TWITCH)],
        fetcher=FakeFetcher(),
        transcriber=FakeTranscriber(),
        _queue=FakeJobQueue(),
    )


@pytest.fixture
def seeded(db: Session) -> Session:
    seed(db, get_settings())
    return db


@pytest.mark.parametrize("url", [YT, TW], ids=["youtube", "twitch"])
def test_pipeline_twice_creates_no_duplicates(
    seeded: Session, container: Container, url: str
) -> None:
    platform = Platform.YOUTUBE if "youtube" in url else Platform.TWITCH
    SourceAdmin(seeded, WS).add(platform, "streamer", ClipPolicy.OPEN_POLICY)
    p = container.ingest_pipeline(seeded, WS)

    p.run_all(url)
    snapshot = {t: count(seeded, t) for t in (m.Video, m.VideoAsset, m.Transcript, m.Job)}
    p.run_all(url)
    assert {t: count(seeded, t) for t in snapshot} == snapshot
    assert snapshot[m.Transcript] == 1
    assert snapshot[m.Job] == 3  # ingest, fetch, transcribe
    assert count(seeded, m.JobRun) == 6  # 2 attempts each; reruns were SKIPPED

    words = seeded.scalars(select(m.Transcript.words)).one()
    assert words[0] == {"w": "no", "s": 1.0, "e": 1.2, "p": 0.99}
    usage = seeded.scalars(select(m.UsageEvent.kind)).all()
    assert sorted(usage) == ["storage_bytes", "transcribe_minutes"]


def test_retire_hides_clips(seeded: Session, container: Container) -> None:
    admin = SourceAdmin(seeded, WS)
    src = admin.add(Platform.YOUTUBE, "streamer", ClipPolicy.OPEN_POLICY)
    vid = container.ingest_pipeline(seeded, WS).ingest(YT).video.id
    prof = seeded.scalars(select(m.ScoringProfile.id)).first()
    cand = m.Candidate(
        workspace_id=WS, video_id=vid, scoring_profile_id=prof, start_s=1, end_s=30, score=0.5
    )
    seeded.add(cand)
    seeded.flush()
    seeded.add(m.Clip(workspace_id=WS, candidate_id=cand.id, start_s=1, end_s=30))
    seeded.flush()

    assert admin.retire(src.id, "creator asked") == 1
    clip = seeded.scalars(select(m.Clip)).one()
    assert clip.deleted_at is not None
    actions = seeded.scalars(select(m.AuditLog.action)).all()
    assert "source.retired" in actions


# ---------------------------------------------------------------- API
@pytest.fixture
def client(seeded: Session, container: Container) -> Iterator[TestClient]:
    app = create_app()

    def _session() -> Iterator[Session]:
        yield seeded

    app.dependency_overrides[db_session] = _session
    app.dependency_overrides[container_dep] = lambda: container
    yield TestClient(app)


def test_api_sources_and_video_flow(client: TestClient, container: Container) -> None:
    r = client.post("/api/v1/videos", json={"url": YT}, headers=AUTH)
    assert r.status_code == 403
    assert "not in allowlist" in r.json()["detail"]

    r = client.post(
        "/api/v1/sources",
        headers=AUTH,
        json={
            "platform": "youtube",
            "channel_handle": "@Streamer",
            "clip_policy": "campaign",
        },
    )
    assert r.status_code == 201
    src = r.json()
    assert src["channel_handle"] == "streamer"

    # campaign without credit → refused until credit is set
    assert client.post("/api/v1/videos", json={"url": YT}, headers=AUTH).status_code == 403
    r = client.patch(
        f"/api/v1/sources/{src['id']}",
        headers=AUTH,
        json={"required_credit_text": "Clip from @Streamer"},
    )
    assert r.status_code == 200

    r = client.post("/api/v1/videos", json={"url": YT}, headers=AUTH)
    assert r.status_code == 201, r.text
    vid = r.json()["id"]
    q = container.queue
    assert isinstance(q, FakeJobQueue)
    assert [j.kind for j in q.jobs] == ["pipeline.fetch"]
    assert q.jobs[0].payload == {"workspace_id": str(WS), "video_id": vid}

    assert client.post("/api/v1/videos", json={"url": YT}, headers=AUTH).status_code == 200

    detail = client.get(f"/api/v1/videos/{vid}", headers=AUTH).json()
    assert [s["stage"] for s in detail["stages"]] == ["ingest"]
    assert len(client.get("/api/v1/videos", headers=AUTH).json()) == 1

    r = client.post(f"/api/v1/sources/{src['id']}/retire", headers=AUTH, json={"reason": "x"})
    assert r.json() == {"clips_hidden": 0}
    r = client.post("/api/v1/videos", json={"url": YT, "override": True}, headers=AUTH)
    assert r.status_code == 403  # takedown beats override


def test_api_rejects_unsupported_url(client: TestClient) -> None:
    r = client.post("/api/v1/videos", json={"url": "https://example.com/x"}, headers=AUTH)
    assert r.status_code == 422


def test_api_requires_auth(client: TestClient) -> None:
    assert client.get("/api/v1/sources").status_code == 401
    assert client.post("/api/v1/videos", json={"url": YT}).status_code == 401


def test_api_rejects_foreign_scoring_profile(client: TestClient, seeded: Session) -> None:
    other = m.Workspace(name="other", slug="other-ws")
    seeded.add(other)
    seeded.flush()
    foreign = m.ScoringProfile(workspace_id=other.id, name="theirs", version=1)
    seeded.add(foreign)
    seeded.flush()
    r = client.post(
        "/api/v1/videos",
        json={"url": YT, "override": True, "scoring_profile_id": str(foreign.id)},
        headers=AUTH,
    )
    assert r.status_code == 404
    assert seeded.scalars(select(m.Video)).first() is None


def test_add_source_backfills_and_retire_covers_override_videos(
    seeded: Session, container: Container
) -> None:
    p = container.ingest_pipeline(seeded, WS)
    vid = p.ingest(YT, override=True).video.id  # ingested before the channel was allowlisted
    video = seeded.get(m.Video, vid)
    assert video is not None and video.source_id is None
    assert video.channel_handle == "streamer"

    admin = SourceAdmin(seeded, WS)
    src = admin.add(Platform.YOUTUBE, "@Streamer", ClipPolicy.OPEN_POLICY)
    seeded.refresh(video)
    assert video.source_id == src.id  # backfilled

    # an override video from the same channel that never got linked is still covered
    seeded.execute(m.Video.__table__.update().where(m.Video.id == vid).values(source_id=None))
    prof = seeded.scalars(select(m.ScoringProfile.id)).first()
    cand = m.Candidate(
        workspace_id=WS, video_id=vid, scoring_profile_id=prof, start_s=1, end_s=30, score=0.5
    )
    seeded.add(cand)
    seeded.flush()
    seeded.add(m.Clip(workspace_id=WS, candidate_id=cand.id, start_s=1, end_s=30))
    seeded.flush()

    assert admin.retire(src.id) == 1
    seeded.refresh(video)
    assert video.status == "source_retired"
    with pytest.raises(SourceNotAllowed):
        p.fetch_audio(vid)
