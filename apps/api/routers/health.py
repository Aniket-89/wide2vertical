"""Liveness, readiness, Prometheus metrics."""

from __future__ import annotations

from typing import Any

import redis
from fastapi import APIRouter, Response
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from apps.api.deps import SettingsDep
from clipforge.infra.db.session import get_engine
from clipforge.infra.metrics import REGISTRY

router = APIRouter(tags=["ops"])


@router.get("/health")
def health() -> dict[str, str]:
    """Liveness: the process is up. No dependencies checked."""
    return {"status": "ok"}


@router.get("/health/ready")
def ready(settings: SettingsDep) -> JSONResponse:
    """Readiness: DB and Redis reachable."""
    checks: dict[str, Any] = {}
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        checks["db"] = "ok"
    except Exception as exc:
        checks["db"] = f"error: {type(exc).__name__}"
    try:
        redis.Redis.from_url(settings.redis_url, socket_timeout=2).ping()
        checks["redis"] = "ok"
    except Exception as exc:
        checks["redis"] = f"error: {type(exc).__name__}"
    ok = all(v == "ok" for v in checks.values())
    return JSONResponse({"status": "ok" if ok else "degraded", **checks}, 200 if ok else 503)


@router.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(REGISTRY), media_type=CONTENT_TYPE_LATEST)
