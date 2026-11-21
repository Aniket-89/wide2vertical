# ruff: noqa: F811  (pytest fixtures imported from test_render_m4 are re-bound as args)
"""M5 API additions against Postgres: profiles/styles, re-analysis, candidate clip status,
publish stub, and the trim-back download fix."""

from __future__ import annotations

from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from clipforge.adapters.fakes.infra import FakeJobQueue
from clipforge.infra.db.render_store import SqlRenderStore
from clipforge.infra.registry import Container
from tests.integration.test_render_m4 import (  # noqa: F401  (fixtures)
    AUTH,
    WS,
    candidate_id,
    client,
    container,
)

pytestmark = pytest.mark.db


def test_scoring_profiles_are_versioned_and_validated(
    client: TestClient, candidate_id: UUID
) -> None:
    base = client.get("/api/v1/scoring-profiles", headers=AUTH).json()
    assert [p["name"] for p in base] == ["default"] and base[0]["is_default"]

    body = {"name": "loud", "weights": {"audio_energy": 1.0}, "params": {"min_clip_s": 15}}
    v1 = client.post("/api/v1/scoring-profiles", json=body, headers=AUTH)
    assert v1.status_code == 201 and v1.json()["version"] == 1
    v2 = client.post("/api/v1/scoring-profiles", json={**body, "is_default": True}, headers=AUTH)
    assert v2.json()["version"] == 2

    rows = client.get("/api/v1/scoring-profiles", headers=AUTH).json()
    assert [(p["name"], p["version"], p["is_default"]) for p in rows] == [
        ("default", 1, False),
        ("loud", 2, True),
        ("loud", 1, False),
    ]
    bad = client.post(
        "/api/v1/scoring-profiles", json={"name": "x", "weights": {"a": 0}}, headers=AUTH
    )
    assert bad.status_code == 422 and "positive" in bad.json()["detail"]


def test_caption_styles_create_and_list(client: TestClient, candidate_id: UUID) -> None:
    r = client.post(
        "/api/v1/caption-styles",
        json={"name": "green", "spec": {"highlight": "#00ff88", "size": 64}},
        headers=AUTH,
    )
    assert r.status_code == 201, r.text
    style = r.json()
    assert style["spec"]["highlight"] == "#00FF88"
    names = [s["name"] for s in client.get("/api/v1/caption-styles", headers=AUTH).json()]
    assert "green" in names
    # the new style can be chosen for a clip
    patch = client.patch(
        f"/api/v1/candidates/{candidate_id}",
        json={"caption_style_id": style["id"]},
        headers=AUTH,
    )
    assert patch.status_code == 200 and "style_changed" in patch.json()["changes"]

    bad = client.post(
        "/api/v1/caption-styles", json={"name": "x", "spec": {"font": "A,{"}}, headers=AUTH
    )
    assert bad.status_code == 422


def test_candidates_carry_clip_status(
    client: TestClient, container: Container, db: Session, candidate_id: UUID
) -> None:
    video_id = SqlRenderStore(db, WS).get_candidate(candidate_id).video_id  # type: ignore[union-attr]
    first = client.get(f"/api/v1/videos/{video_id}/candidates", headers=AUTH).json()
    assert first[0]["clip"] is None

    clip = client.post(f"/api/v1/candidates/{candidate_id}/preview", headers=AUTH).json()
    container.render_pipeline(db, WS).render(UUID(clip["id"]), "preview")
    after = client.get(f"/api/v1/videos/{video_id}/candidates", headers=AUTH).json()
    assert after[0]["clip"] == {"clip_id": clip["id"], "preview": "succeeded", "final": None}
    detail = client.get(f"/api/v1/clips/{clip['id']}", headers=AUTH).json()
    assert detail["render_status"] == {"preview": "succeeded", "final": None}


def test_reanalyze_pins_profile_and_queues_job(
    client: TestClient, container: Container, db: Session, candidate_id: UUID
) -> None:
    video_id = SqlRenderStore(db, WS).get_candidate(candidate_id).video_id  # type: ignore[union-attr]
    prof = client.post(
        "/api/v1/scoring-profiles",
        json={"name": "loud", "weights": {"audio_energy": 1.0}},
        headers=AUTH,
    ).json()
    r = client.post(
        f"/api/v1/videos/{video_id}/analyze",
        json={"scoring_profile_id": prof["id"]},
        headers=AUTH,
    )
    assert r.status_code == 202 and r.json()["scoring_profile_id"] == prof["id"]
    q = container.queue
    assert isinstance(q, FakeJobQueue)
    job = q.jobs[-1]
    assert job.kind == "pipeline.analyze" and job.payload["scoring_profile_id"] == prof["id"]
    # candidates now shown are the pinned profile's (none yet): no mixing of score scales
    assert client.get(f"/api/v1/videos/{video_id}/candidates", headers=AUTH).json() == []

    missing = client.post(
        f"/api/v1/videos/{video_id}/analyze",
        json={"scoring_profile_id": str(UUID(int=7))},
        headers=AUTH,
    )
    assert missing.status_code == 404


def test_publish_is_501_until_m6(client: TestClient) -> None:
    r = client.post(f"/api/v1/clips/{UUID(int=1)}/publish", headers=AUTH)
    assert r.status_code == 501


def test_trim_back_serves_the_matching_render(
    client: TestClient, container: Container, db: Session, candidate_id: UUID
) -> None:
    """Trim A -> B -> A: the cached render for A must become the downloadable one again."""
    pipe = container.render_pipeline(db, WS)
    clip = pipe.ensure_clip(candidate_id)
    a = pipe.render(clip.id, "preview").render
    client.patch(f"/api/v1/candidates/{candidate_id}", json={"start_s": 102.0}, headers=AUTH)
    db.commit()
    b = pipe.render(clip.id, "preview").render
    assert a.render_hash != b.render_hash
    client.patch(f"/api/v1/candidates/{candidate_id}", json={"start_s": 100.0}, headers=AUTH)
    db.commit()
    again = pipe.render(clip.id, "preview")
    assert again.cached and again.render.render_hash == a.render_hash
    latest = SqlRenderStore(db, WS).latest_render(clip.id, "preview")
    assert latest is not None and latest.render_hash == a.render_hash
