"""M4 against Postgres: SqlRenderStore, review/render/download API, feedback events."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from apps.api.deps import container_dep, db_session
from apps.api.main import create_app
from clipforge.adapters.blobstore.local import LocalBlobStore
from clipforge.adapters.eventbus.in_process import InProcessEventBus
from clipforge.adapters.fakes.infra import FakeClock, FakeJobQueue
from clipforge.adapters.fakes.pipeline import (
    FakeFacecamDetector,
    FakeFetcher,
    FakeRenderer,
    FakeResolver,
    FakeSectionFetcher,
    FakeTranscriber,
)
from clipforge.adapters.misc import AllowAllQuota
from clipforge.domain.values import ClipPolicy, Platform, WorkspaceId
from clipforge.infra.config import get_settings
from clipforge.infra.db import models as m
from clipforge.infra.db.render_store import SqlRenderStore
from clipforge.infra.db.sources import SourceAdmin
from clipforge.infra.registry import Container
from clipforge.infra.seed import seed
from clipforge.ports.analysis import CandidateWindow
from clipforge.ports.pipeline import ResolvedVideo, Transcript, Word

pytestmark = pytest.mark.db

WS = WorkspaceId(UUID(get_settings().default_workspace_id))
AUTH = {"Authorization": "Bearer test-token"}
WORDS = tuple(Word(f"w{i}", 100 + i * 0.5, 100.4 + i * 0.5, 1.0) for i in range(80))


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
        _queue=FakeJobQueue(),
        sections=FakeSectionFetcher(),
        detector=FakeFacecamDetector(),
        renderer=FakeRenderer(),
    )


@pytest.fixture
def candidate_id(db: Session) -> UUID:
    seed(db, get_settings())
    src = SourceAdmin(db, WS).add(
        Platform.TWITCH,
        "streamer",
        ClipPolicy.CAMPAIGN,
        required_credit_text="Clip: twitch.tv/streamer",
    )
    store = SqlRenderStore(db, WS)
    rv = ResolvedVideo(
        Platform.TWITCH, "https://www.twitch.tv/videos/42", "42", "streamer", None, "t", 3600.0
    )
    v, _ = store.upsert_video(rv, src.id, False)
    store.save_transcript(v.id, "h", "c", Transcript("en", 3600, "m", WORDS))
    prof = store.get_scoring_profile(None)
    store.replace_candidates(
        v.id, prof.id, [CandidateWindow(100.0, 128.0, 120.0, 0.9, {"audio_energy": 0.8}, "loud")]
    )
    return store.list_candidates(v.id)[0].id


@pytest.fixture
def client(db: Session, container: Container) -> Iterator[TestClient]:
    app = create_app()

    def _session() -> Iterator[Session]:
        yield db

    app.dependency_overrides[db_session] = _session
    app.dependency_overrides[container_dep] = lambda: container
    yield TestClient(app)


def test_review_render_download_flow(
    client: TestClient, container: Container, db: Session, candidate_id: UUID
) -> None:
    q = container.queue
    assert isinstance(q, FakeJobQueue)

    r = client.post(f"/api/v1/candidates/{candidate_id}/preview", headers=AUTH)
    assert r.status_code == 202
    clip = r.json()
    assert clip["credit_text"] == "Clip: twitch.tv/streamer" and clip["layout"] == "auto"
    assert clip["warnings"]  # reused-content warning until an intro is added
    assert [j.kind for j in q.jobs] == ["render.preview"]

    assert (
        client.get(f"/api/v1/clips/{clip['id']}/download?kind=preview", headers=AUTH).status_code
        == 404
    )  # not rendered yet
    assert (
        client.post(
            f"/api/v1/clips/{clip['id']}/render", json={"kind": "final"}, headers=AUTH
        ).status_code
        == 409
    )  # final needs approval

    # what the worker does for that job
    container.render_pipeline(db, WS).render(UUID(clip["id"]), "preview")
    dl = client.get(f"/api/v1/clips/{clip['id']}/download?kind=preview", headers=AUTH)
    assert dl.status_code == 200 and dl.content == b"fake-mp4:preview"
    assert dl.headers["content-type"] == "video/mp4"

    r = client.patch(
        f"/api/v1/candidates/{candidate_id}",
        headers=AUTH,
        json={
            "status": "approved",
            "start_s": 101.0,
            "end_s": 127.0,
            "commentary_text": "My reaction",
        },
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "approved" and body["final_render_queued"]
    assert set(body["changes"]) == {"trimmed", "commentary_set", "approved"}
    assert body["clip"]["warnings"] == []
    assert q.jobs[-1].kind == "render.final"

    kinds = sorted(db.scalars(select(m.FeedbackEvent.kind)))
    assert kinds == ["approved", "commentary_set", "trimmed"]

    detail = client.get(f"/api/v1/clips/{clip['id']}", headers=AUTH).json()
    assert detail["start_s"] == 101.0
    assert detail["export_description"].endswith("Clip: twitch.tv/streamer")


def test_invalid_review_is_422_and_unknown_is_404(client: TestClient, candidate_id: UUID) -> None:
    bad = client.patch(
        f"/api/v1/candidates/{candidate_id}", headers=AUTH, json={"start_s": 100.0, "end_s": 101.0}
    )
    assert bad.status_code == 422 and "clip length" in bad.json()["detail"]
    missing = client.patch(
        f"/api/v1/candidates/{UUID(int=5)}", headers=AUTH, json={"status": "approved"}
    )
    assert missing.status_code == 404


def test_clips_are_workspace_scoped(
    client: TestClient, container: Container, db: Session, candidate_id: UUID
) -> None:
    clip = container.render_pipeline(db, WS).ensure_clip(candidate_id)
    other = WorkspaceId(UUID(int=99))
    db.add(m.Workspace(id=other, name="other", slug="other-ws-m4"))
    db.flush()
    assert SqlRenderStore(db, other).get_clip(clip.id) is None
    assert SqlRenderStore(db, other).get_candidate(candidate_id) is None


def test_requires_auth(client: TestClient, candidate_id: UUID) -> None:
    assert client.patch(f"/api/v1/candidates/{candidate_id}", json={}).status_code == 401
    assert client.get(f"/api/v1/clips/{UUID(int=1)}/download").status_code == 401
