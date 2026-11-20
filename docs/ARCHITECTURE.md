# Architecture

ClipForge is a **modular monolith** with hexagonal (ports & adapters) boundaries:
one codebase, one Python image that runs as `api`, `worker`, or `worker-gpu`.

```
            ┌──────────── apps/ ─────────────┐
 HTTP ───▶  │ api (FastAPI)  cli (Typer)     │
 Redis ──▶  │ worker (Dramatiq dispatch)     │
            └──────────────┬─────────────────┘
                           │ calls
            ┌──────────────▼─────────────────┐
            │ services/  (use cases)         │   signals/ scoring/ compliance/
            └──────────────┬─────────────────┘
                           │ depends on
            ┌──────────────▼─────────────────┐
            │ ports/  (Protocols)            │──▶ domain/ (pure: values, events, ids)
            └──────────────▲─────────────────┘
                           │ implemented by
            ┌──────────────┴─────────────────┐
            │ adapters/  (+ fakes/)          │◀── wired ONLY by infra/registry.py
            └────────────────────────────────┘
            infra/: config, db (models, repositories, session), logging, metrics, seed
```

## Boundaries (enforced in CI by `import-linter`)

| Contract | Rule |
|---|---|
| Domain is pure | `clipforge.domain` imports no other internal package, no SQLAlchemy, no FastAPI |
| Core never imports adapters or infra | `ports`, `services`, `signals`, `scoring`, `compliance` ↛ `adapters`, `infra`, `apps` |
| Only the registry wires adapters | `apps` reaches adapters only through `clipforge.infra.registry` |

## Multi-tenancy

- Every business table has `workspace_id` (test: `test_every_business_table_has_workspace_id`).
- `WorkspaceRepository` puts `workspace_id` into every query and rejects writes that carry another
  workspace's id (tests: `test_no_cross_workspace_reads`, `test_cross_workspace_write_is_refused`).
- `BlobKey` cannot be constructed without a `ws/<uuid>/` prefix.
- Auth seam: `apps.api.deps.current_principal` → `Principal(user_id, workspace_id, role)`.
  MVP = one static bearer token mapped to the seeded workspace.

## Jobs

`JobQueue.enqueue(kind, payload, priority)` → Dramatiq message to queue `<base>[_high|_low]` →
the single `clipforge_dispatch` actor → `HANDLERS[kind](payload)`. Redis has no per-message
priority, so priorities are separate queues. Transcription gets its own queue family
(`transcribe*`) consumed by `worker-gpu`, so GPU capacity scales independently.

## HTTP surface (M0)

| Path | Auth | Purpose |
|---|---|---|
| `GET /health` | none | liveness, no dependencies |
| `GET /health/ready` | none | DB + Redis reachability (503 when degraded) |
| `GET /metrics` | none | Prometheus |
| `GET /api/v1/me` | bearer | auth-seam check |
| `GET /api/v1/openapi.json`, `/api/v1/docs` | none | OpenAPI |

## Running locally

```bash
cp .env.example .env
docker compose up -d postgres redis
uv sync
uv run alembic upgrade head && uv run clipforge seed
uv run uvicorn apps.api.main:app --reload
uv run pytest
# or everything in containers:
docker compose up --build
```

Windows notes: use `127.0.0.1`, not `localhost` (ADR-0007). Host-side Redis is on port 6380 (ADR-0009).
