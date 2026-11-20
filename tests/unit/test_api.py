from fastapi.testclient import TestClient

from apps.api.main import create_app

client = TestClient(create_app())


def test_health_is_dependency_free() -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_metrics_exposes_prometheus() -> None:
    client.get("/health")
    r = client.get("/metrics")
    assert r.status_code == 200
    assert "clipforge_http_requests_total" in r.text


def test_auth_seam_rejects_missing_and_wrong_token() -> None:
    assert client.get("/api/v1/me").status_code == 401
    r = client.get("/api/v1/me", headers={"Authorization": "Bearer nope"})
    assert r.status_code == 401


def test_auth_seam_returns_principal() -> None:
    r = client.get("/api/v1/me", headers={"Authorization": "Bearer test-token"})
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "owner"
    assert body["workspace_id"] == "01936f00-0000-7000-8000-000000000001"


def test_openapi_is_versioned() -> None:
    assert client.get("/api/v1/openapi.json").status_code == 200
