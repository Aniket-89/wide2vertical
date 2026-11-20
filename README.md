# ClipForge (wide2vertical)

Takes a YouTube / Twitch / Kick VOD, finds moments with viral potential, and cuts them into
vertical 9:16 clips with word-by-word captions for review and download.

Personal tool first, with SaaS-ready seams. The full spec is in `CLAUDE CODE PROMPT.md`, and the plan is in `docs/PLAN.md`.

## Status

| Milestone | State |
|---|---|
| M0 Skeleton | ✅ |
| M1 Ingest + transcript | next |

## Quick start

```bash
cp .env.example .env            # set CLIPFORGE_API_TOKEN
docker compose up --build       # postgres, redis, migrate+seed, api, worker
curl http://127.0.0.1:8000/health
curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/api/v1/me
```

Dev loop without containers for the app itself:

```bash
docker compose up -d postgres redis
uv sync && uv run alembic upgrade head && uv run clipforge seed
uv run pytest && uv run ruff check . && uv run mypy clipforge apps && uv run lint-imports
```

See `docs/ARCHITECTURE.md`, `docs/DECISIONS.md`, `docs/SAAS_READINESS.md`.
