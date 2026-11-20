"""Prometheus metrics. One registry, declared here so names stay consistent."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

REGISTRY = CollectorRegistry()

STAGE_DURATION = Histogram(
    "clipforge_stage_duration_seconds",
    "Pipeline stage duration",
    ["stage"],
    registry=REGISTRY,
    buckets=(1, 5, 15, 30, 60, 120, 300, 600, 1800, 3600),
)
STAGE_FAILURES = Counter(
    "clipforge_stage_failures_total", "Pipeline stage failures", ["stage"], registry=REGISTRY
)
QUEUE_DEPTH = Gauge("clipforge_queue_depth", "Jobs waiting per queue", ["queue"], registry=REGISTRY)
LLM_TOKENS = Counter(
    "clipforge_llm_tokens_total", "LLM tokens used", ["direction"], registry=REGISTRY
)
RENDER_SECONDS = Counter(
    "clipforge_render_seconds_total", "Wall-clock seconds spent rendering", registry=REGISTRY
)
HTTP_REQUESTS = Counter(
    "clipforge_http_requests_total",
    "HTTP requests",
    ["method", "route", "status"],
    registry=REGISTRY,
)
