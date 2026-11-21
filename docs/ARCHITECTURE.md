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

## Pipeline (M1)

```
POST /videos or `clipforge ingest URL`
  -> ingest     resolve (yt-dlp -J) -> allowlist.decide -> upsert video   [sync]
  -> fetch      yt-dlp bestaudio -> BlobStore ws/<ws>/videos/<id>/audio.* [queue: default]
  -> transcribe faster-whisper, word timestamps -> transcripts.words      [queue: transcribe]
```

- Each stage = one `jobs` row per (video, stage) and one `job_runs` row per attempt.
- Cache keys: fetch reuses the audio asset if its blob still exists, and transcribe reuses a
  transcript with the same `(audio content_hash, transcriber.config_version)`. Rerunning
  produces no duplicate rows (integration test `test_pipeline_twice_creates_no_duplicates`).
- Metering: `usage_events` gets `storage_bytes` (audio) and `transcribe_minutes`.
- Degradation: a whisper OOM or CUDA error retries once on CPU/int8; stage failures are recorded
  with the error (ADR-0012) and published as `StageFailed`.
- Transcript word format: `{"w": text, "s": start_s, "e": end_s, "p": probability}`.

## Analysis (M2)

```
pipeline.transcribe done -> pipeline.analyze (worker) or `clipforge analyze <video_id>`
  -> signals     chat (ChatProvider, flagged, fail-soft; cached as chat_dump asset)
                 audio loudness (AudioAnalyzer: PyAV, streamed; cached as _audio_rms_db series)
                 signals/*.py -> 0..1 per second -> signal_series (cached by input hash)
  -> candidates  scoring/: combine -> find_peaks -> make_window (snap) -> nms -> candidates
```

| Signal | Needs | Idea |
|---|---|---|
| `chat_velocity` | chat (>= 50 msgs) | msgs/s vs rolling 300 s baseline, shifted 6 s earlier |
| `emote_burst` | chat | share of emote/spam msgs above the usual share, volume-weighted |
| `audio_energy` | audio | RMS dBFS vs 60 s baseline + sudden jumps |
| `speech_intensity` | transcript (required) | words/s vs 120 s baseline + exclamations/hype words |

Design notes are in ADR-0023. Replay harness: `clipforge eval --labels tests/fixtures/labels.json
[--profile p] [--weight name=v] [--param name=v] [--k 5 --k 10]` reports precision@K and
recall@K without writing candidates.

## LLM judge (M3)

```
candidates (pending, by signal score)
  -> top judge_top_n_per_hour x hours (cap 150)
  -> per window: transcript excerpt + signal hints -> cache lookup (BlobStore judge-cache/)
  -> uncached, batched by 8 -> LLMJudge (AnthropicJudge | OpenAICompatJudge [OpenRouter, Groq,
                               any OpenAI-compatible URL] | NullJudge | FakeJudge)
  -> final score = blend(signal, judge.overall, w); titles[3], hook, reason, verdict JSON
  -> rerank; usage_events: llm_tokens_in / llm_tokens_out / llm_cost_usd_est
```

Runs as stage `judge` after `candidates` in `pipeline.analyze` (worker) and `clipforge analyze`
(`--no-judge` to skip). Off unless `CLIPFORGE_ENABLE_LLM_JUDGE=true` and the provider's key are
both set; `CLIPFORGE_LLM_PROVIDER` picks the adapter (ADR-0030). Prompts: `docs/PROMPTS.md`. Design: ADR-0027..0029.

## Render & review (M4)

```
candidate --(preview / approve)--> clip (layout auto, credit from source, intro, caption edits)
  -> section   YouTube: yt-dlp --download-sections | Twitch/Kick: HLS segment fetch (cached)
  -> layout    OpenCV faces -> rendering/layout.py (facecam / center_crop / blur bg | override)
  -> captions  rendering/captions.py: word-by-word ASS from Whisper words (+ credit, intro)
  -> render    FfmpegRenderer: cut + reframe + burn captions + loudnorm -> MP4
  -> blob ws/<ws>/clips/<clip>/<kind>-<hash>.mp4, clip_renders row, usage render_seconds
review: PATCH /candidates/{id} -> ReviewService -> feedback_events (+ final render job)
```

Worker: `pipeline.analyze` -> `render.preview_top` -> one `render.preview` per clip
(queue `render`); approval -> `render.final`. Design: ADR-0031..0034.

## HTTP surface

| Path | Auth | Purpose |
|---|---|---|
| `GET /health` | none | liveness, no dependencies |
| `GET /health/ready` | none | DB + Redis reachability (503 when degraded) |
| `GET /metrics` | none | Prometheus |
| `GET /api/v1/me` | bearer | auth-seam check |
| `POST /api/v1/videos` | bearer | `{url, override?, scoring_profile_id?}` -> 201 new / 200 existing / 403 refused / 422 unsupported |
| `GET /api/v1/videos`, `GET /api/v1/videos/{id}` | bearer | list (limit/offset); detail with per-stage status |
| `GET/POST /api/v1/sources`, `PATCH /api/v1/sources/{id}` | bearer | allowlist management |
| `POST /api/v1/sources/{id}/retire` | bearer | takedown: block the source and hide its clips |
| `PATCH /api/v1/candidates/{id}` | bearer | approve/reject/trim/caption/layout/crop/intro; queues the final render when needed |
| `POST /api/v1/candidates/{id}/preview` | bearer | create the clip and queue a preview |
| `GET /api/v1/clips/{id}` | bearer | clip + renders + export description (with credit) + warnings |
| `POST /api/v1/clips/{id}/render` | bearer | `{kind: preview|final, force}`; final needs approval (409) |
| `GET /api/v1/clips/{id}/download?kind=` | bearer | the rendered MP4 |
| `GET /api/v1/videos/{id}/candidates` | bearer | ranked candidates: per-signal components, reason, judge verdict, `title_suggestions`, `hook_text` |
| `GET /api/v1/openapi.json`, `/api/v1/docs` | none | OpenAPI |

## Running locally

```bash
cp .env.example .env
docker compose up -d postgres redis
uv sync
uv run alembic upgrade head && uv run clipforge seed
uv run uvicorn apps.api.main:app --reload
uv run clipforge sources add twitch somestreamer --policy campaign --credit "Clip: @somestreamer"
uv run clipforge ingest https://www.twitch.tv/videos/123   # add --override for non-allowlisted
uv run clipforge status <video_id>
uv run clipforge analyze <video_id> [--profile p] [--dry-run]   # signals + candidates
uv run clipforge candidates <video_id>
uv run clipforge eval --labels tests/fixtures/labels.json
uv run clipforge preview <candidate_id>            # 540x960 preview
uv run clipforge approve <candidate_id> [--start --end --caption --layout --intro]  # + final render
uv run clipforge reject <candidate_id>
uv run clipforge export <clip_id> --out clip.mp4   # prints the post description incl. credit
uv run pytest                          # CLIPFORGE_NETWORK_TESTS=1 also runs real yt-dlp/whisper
# or everything in containers:
docker compose up --build
```

Windows notes: use `127.0.0.1`, not `localhost` (ADR-0007). Host-side Redis is on port 6380 (ADR-0009).
