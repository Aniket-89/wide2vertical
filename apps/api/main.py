"""FastAPI app factory."""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

import structlog
from fastapi import FastAPI, Request, Response

from apps.api.routers import clips, config, health, me, sources, videos
from clipforge.infra.config import get_settings
from clipforge.infra.logging import configure_logging
from clipforge.infra.metrics import HTTP_REQUESTS

log = structlog.get_logger("api")


def create_app() -> FastAPI:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)

    app = FastAPI(
        title="ClipForge",
        version="0.1.0",
        openapi_url="/api/v1/openapi.json",
        docs_url="/api/v1/docs",
    )

    @app.middleware("http")
    async def access_log(
        request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        t0 = time.perf_counter()
        response = await call_next(request)
        route = request.scope.get("route")
        route_path = getattr(route, "path", "unmatched")
        HTTP_REQUESTS.labels(request.method, route_path, str(response.status_code)).inc()
        log.info(
            "http_request",
            method=request.method,
            route=route_path,
            status=response.status_code,
            ms=round((time.perf_counter() - t0) * 1000, 1),
        )
        return response

    app.include_router(health.router)
    app.include_router(me.router, prefix="/api/v1")
    app.include_router(sources.router, prefix="/api/v1")
    app.include_router(videos.router, prefix="/api/v1")
    app.include_router(clips.router, prefix="/api/v1")
    app.include_router(config.router, prefix="/api/v1")
    return app


app = create_app()
