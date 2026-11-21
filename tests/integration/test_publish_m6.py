# ruff: noqa: F811  (pytest fixtures imported from test_render_m4 are re-bound as args)
"""M6 against Postgres: publish policy, idempotency, scheduling, takedown, stats, autopublish."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from clipforge.adapters.fakes.infra import FakeJobQueue
from clipforge.adapters.fakes.publish import FakePublisher
from clipforge.adapters.secrets.aesgcm import AesGcmSecretBox, generate_key
from clipforge.domain.values import ClipPolicy
from clipforge.infra.db import models as m
from clipforge.infra.db.publish_store import SqlPublishStore
from clipforge.infra.db.sources import SourceAdmin
from clipforge.infra.registry import Container
from clipforge.ports.publish import PublishError
from clipforge.services.publish import PublishConfig, PublishService
from tests.integration.test_render_m4 import (  # noqa: F401  (fixtures)
    AUTH,
    WS,
    candidate_id,
    client,
    container,
)

pytestmark = pytest.mark.db
CREDS = {"client_id": "c", "client_secret": "s", "refresh_token": "r"}


@pytest.fixture
def pub(container: Container) -> FakePublisher:
    fake = FakePublisher()
    container.publishers = {"youtube": fake}
    container.secrets = AesGcmSecretBox(generate_key())
    return fake


def _connect(db: Session, container: Container) -> UUID:
    assert container.secrets is not None
    ref = container.secrets.seal(WS, json.dumps(CREDS).encode())
    return SqlPublishStore(db, WS).add_target("youtube", "My channel", ref).id


def _approve_and_render(client: TestClient, container: Container, db: Session, cand: UUID) -> UUID:
    r = client.patch(f"/api/v1/candidates/{cand}", json={"status": "approved"}, headers=AUTH)
    clip_id = UUID(r.json()["clip"]["id"])
    container.render_pipeline(db, WS).render(clip_id, "final")  # what the worker does
    return clip_id


def _svc(container: Container, db: Session, **cfg: object) -> PublishService:
    svc = container.publish_service(db, WS)
    if cfg:
        svc.cfg = PublishConfig(**cfg)  # type: ignore[arg-type]
    return svc


def test_publish_requires_approval_target_and_final_render(
    client: TestClient, container: Container, db: Session, candidate_id: UUID, pub: FakePublisher
) -> None:
    clip = client.post(f"/api/v1/candidates/{candidate_id}/preview", headers=AUTH).json()
    r = client.post(f"/api/v1/clips/{clip['id']}/publish", json={}, headers=AUTH)
    assert r.status_code == 409 and "connect a YouTube channel" in r.json()["detail"]
    _connect(db, container)
    r = client.post(f"/api/v1/clips/{clip['id']}/publish", json={}, headers=AUTH)
    assert r.status_code == 409 and "approve" in r.json()["detail"]
    client.patch(f"/api/v1/candidates/{candidate_id}", json={"status": "approved"}, headers=AUTH)
    r = client.post(f"/api/v1/clips/{clip['id']}/publish", json={"title": "t"}, headers=AUTH)
    assert r.status_code == 409 and "final render" in r.json()["detail"]
    assert pub.uploads == []


def test_publish_now_uploads_once_with_credit_and_tracks_stats(
    client: TestClient, container: Container, db: Session, candidate_id: UUID, pub: FakePublisher
) -> None:
    _connect(db, container)
    clip_id = _approve_and_render(client, container, db, candidate_id)
    body = {"title": "Best <play> ever", "description": "watch this", "privacy": "unlisted"}
    r = client.post(f"/api/v1/clips/{clip_id}/publish", json=body, headers=AUTH)
    assert r.status_code == 202, r.text
    out = r.json()
    assert out["status"] == "queued" and out["title"] == "Best play ever"
    assert out["description"] == "watch this\n\nClip: twitch.tv/streamer"  # credit always added
    q = container.queue
    assert isinstance(q, FakeJobQueue) and q.jobs[-1].kind == "publish.run"

    done = _svc(container, db).run(UUID(out["id"]))  # what the worker does
    assert done is not None and done.status == "published" and done.external_id == "yt1"
    req, creds, data = pub.uploads[0]
    assert json.loads(creds) == CREDS and data == b"fake-mp4:final" and req.privacy == "unlisted"

    # double click / repeated request: same publication, no second upload or job
    jobs_before = len(q.jobs)
    again = client.post(f"/api/v1/clips/{clip_id}/publish", json=body, headers=AUTH).json()
    assert again["id"] == out["id"] and again["status"] == "published"
    assert len(q.jobs) == jobs_before
    assert _svc(container, db).run(UUID(out["id"])).status == "published"  # type: ignore[union-attr]
    assert len(pub.uploads) == 1

    pub.views["yt1"] = 1234
    assert _svc(container, db).refresh_stats() == 1
    listed = client.get(f"/api/v1/publications?clip_id={clip_id}", headers=AUTH).json()
    assert listed[0]["stats"]["views"] == 1234 and listed[0]["url"] == "https://youtu.be/yt1"
    kinds = set(db.scalars(select(m.FeedbackEvent.kind)))
    assert {"publish_scheduled", "published", "performance"} <= kinds


def test_scheduled_post_waits_and_can_be_cancelled(
    client: TestClient, container: Container, db: Session, candidate_id: UUID, pub: FakePublisher
) -> None:
    _connect(db, container)
    clip_id = _approve_and_render(client, container, db, candidate_id)
    later = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    out = client.post(
        f"/api/v1/clips/{clip_id}/publish", json={"title": "t", "scheduled_at": later}, headers=AUTH
    ).json()
    assert out["status"] == "scheduled"
    assert _svc(container, db).claim_due() == []  # not due yet
    r = client.post(f"/api/v1/publications/{out['id']}/cancel", headers=AUTH)
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    assert client.post(f"/api/v1/publications/{out['id']}/cancel", headers=AUTH).status_code == 409
    # re-scheduling a cancelled post reuses its row
    again = client.post(f"/api/v1/clips/{clip_id}/publish", json={"title": "t2"}, headers=AUTH)
    assert again.json()["id"] == out["id"] and again.json()["status"] == "queued"


def test_takedown_after_scheduling_stops_the_upload(
    client: TestClient, container: Container, db: Session, candidate_id: UUID, pub: FakePublisher
) -> None:
    target = _connect(db, container)
    clip_id = _approve_and_render(client, container, db, candidate_id)
    svc = _svc(container, db)
    p = svc.schedule(clip_id, target, title="t", at=container.clock.now() - timedelta(minutes=1))
    assert svc.claim_due() == [p.id]
    src = db.scalars(select(m.Source)).one()
    SourceAdmin(db, WS).retire(src.id, "streamer asked")
    out = svc.run(p.id)
    assert out is not None and out.status == "failed" and "takedown" in (out.error or "")
    assert pub.uploads == []


def test_a_crash_marks_failed_and_is_never_retried_into_a_double_post(
    client: TestClient, container: Container, db: Session, candidate_id: UUID, pub: FakePublisher
) -> None:
    target = _connect(db, container)
    clip_id = _approve_and_render(client, container, db, candidate_id)
    svc = _svc(container, db)
    p = svc.schedule(clip_id, target, title="t")
    pub.crash = True
    with pytest.raises(PublishError):  # surfaces as an expected failure: Dramatiq won't retry
        svc.run(p.id)
    pub.crash = False
    again = svc.run(p.id)  # a duplicate delivery of the same job
    assert again is not None and again.status == "failed" and pub.uploads == []


def test_unknown_source_policy_cannot_publish(
    client: TestClient, container: Container, db: Session, candidate_id: UUID, pub: FakePublisher
) -> None:
    target = _connect(db, container)
    clip_id = _approve_and_render(client, container, db, candidate_id)
    src = db.scalars(select(m.Source)).one()
    SourceAdmin(db, WS).update(src.id, clip_policy=ClipPolicy.UNKNOWN)
    with pytest.raises(Exception, match="clip permission"):
        _svc(container, db).schedule(clip_id, target, title="t")


def test_autopublish_only_for_own_channel_and_only_when_enabled(
    client: TestClient, container: Container, db: Session, candidate_id: UUID, pub: FakePublisher
) -> None:
    _connect(db, container)
    clip_id = _approve_and_render(client, container, db, candidate_id)
    # needs a title: the judge is off in tests, so give the candidate a suggestion
    cand = db.get(m.Candidate, candidate_id)
    assert cand is not None
    cand.title_suggestions = ["Auto title"]
    db.flush()
    assert _svc(container, db, autopublish=False).maybe_autopublish(clip_id) is None
    assert _svc(container, db, autopublish=True).maybe_autopublish(clip_id) is None  # campaign
    src = db.scalars(select(m.Source)).one()
    SourceAdmin(db, WS).update(src.id, clip_policy=ClipPolicy.OWN_CHANNEL)
    p = _svc(container, db, autopublish=True).maybe_autopublish(clip_id)
    assert p is not None and p.title == "Auto title" and p.privacy == "private"
