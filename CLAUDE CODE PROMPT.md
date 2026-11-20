# Project: ClipForge — AI stream clipping engine (personal tool, SaaS-ready architecture)

You are building **ClipForge**, a tool that takes a YouTube / Twitch / Kick stream or VOD URL, finds moments with viral potential, cuts them into vertical (9:16) captioned clips, and lets me review and download them (later: auto-post to YouTube/Instagram/TikTok).

It is **for my own use first**. It may become a SaaS later **only if it proves profitable**. So: build a simple single-user MVP, but make the architecture clean enough that multi-tenancy, billing and scaling can be added later **without rewrites**. Do not build SaaS features now. Do build the seams for them.

---

## 0. How to work

1. **Start in plan mode.** Before writing code, produce a short plan: repo layout, module boundaries, data model, and the milestone order below. Wait for my approval of the plan.
2. Build milestone by milestone. Each milestone must run end-to-end and have tests before moving on.
3. Prefer boring, well-known tools. No premature microservices. No Kubernetes. Docker Compose only.
4. Keep every external dependency behind an interface (see "Ports and adapters"). This is the most important rule.
5. After each milestone: run the tests, write a short summary of what works, what doesn't, and what's next. Update `docs/ARCHITECTURE.md` and `docs/DECISIONS.md` (ADR-style, one short entry per non-obvious decision).
6. Ask me before: adding a paid service, adding a dependency with a non-permissive licence, or changing the data model after Milestone 2.

## 1. Goals and non-goals

**Goals (MVP)**
- Input: one or more stream/VOD URLs (YouTube, Twitch, Kick).
- Output: ranked candidate clips (20–60 s), vertical 9:16, burned-in word-by-word captions, with a score and a short "why this was picked" explanation.
- Review UI: watch, approve/reject, trim start/end, edit caption text, download MP4.
- Manual-publish first. Auto-publish is a later milestone behind a flag.

**Non-goals (for now)**
- User accounts, billing, teams, public signup. (Schema and code must be *ready* for them, not implement them.)
- Real-time live-stream clipping. VOD first; live comes in Milestone 7.
- Trained ML models. Use heuristics + an LLM judge; log data so models can be trained later.

## 2. Compliance rules (build these in, not as an afterthought)

Clipping other people's streams has copyright/platform risk. The tool must make the safe path the default:

- **Source allowlist.** A `sources` table of channels/streamers with fields: `platform`, `channel_handle`, `clip_policy` (`campaign` | `written_permission` | `open_policy` | `own_channel` | `unknown`), `policy_url`, `required_credit_text`, `notes`. By default the tool **refuses to process a URL whose channel is not in the allowlist or is `unknown`**, unless I pass an explicit `--override` flag (logged).
- **Credit.** Each clip stores `credit_text` (from the source's required credit) and the exported description/caption includes it.
- **Takedown.** A one-click "retire this source" that marks it blocked and hides its clips.
- **YouTube reused-content note.** YouTube can refuse monetization for reused content even with permission. The review UI has an optional "commentary / intro overlay" field, and a warning when a clip has no added edits.
- **No view-botting, no fake engagement, no mass multi-account posting features.** Do not build them.
- Respect each platform's ToS for downloads and API usage. Do not add scraping that evades authentication or rate limits.

## 3. Tech stack (defaults; justify any change in DECISIONS.md)

| Concern | Choice |
|---|---|
| Language | Python 3.12 (backend + workers) |
| API | FastAPI + Pydantic v2 |
| DB | PostgreSQL 16 (SQLAlchemy 2 + Alembic). TimescaleDB extension optional for chat/audio time-series. |
| Queue | Redis + a task runner (ARQ or Dramatiq; pick one and wrap it in a `JobQueue` port) |
| Storage | Local disk by default, behind a `BlobStore` port (S3/MinIO adapter later) |
| Transcription | `faster-whisper` (GPU if present, CPU fallback), word-level timestamps |
| Video | `ffmpeg` (subprocess, no heavy wrappers), `yt-dlp` (VOD/YT), `streamlink` (live, later) |
| Face/layout | MediaPipe or OpenCV for facecam detection and crop |
| LLM judge | Anthropic API behind an `LLMJudge` port (model name in config; cheap model for scoring) |
| Frontend | React + Vite + TypeScript, minimal UI. No heavy UI framework. |
| Packaging | Docker Compose: `api`, `worker`, `postgres`, `redis`, `web` |
| Tooling | `uv` or `poetry`, `ruff`, `mypy`, `pytest`, `pre-commit` |

## 4. Architecture

A **modular monolith**: one codebase, one deployable API + worker image, strict internal module boundaries. Modules talk through interfaces and domain events, never by reaching into each other's tables or internals.

### 4.1 Ports and adapters (hexagonal)

Core domain code depends only on these interfaces. Each has a default adapter plus a fake for tests.

- `SourceResolver` — URL → platform, channel, video metadata. Adapters: `YouTubeResolver`, `TwitchResolver`, `KickResolver`.
- `MediaFetcher` — download VOD/audio/segments. Adapter: `YtDlpFetcher`. Later: `StreamlinkLiveFetcher`.
- `ChatProvider` — chat replay as timestamped messages. Adapters: `TwitchChatReplay`, `KickChatReplay` (the Kick API is unofficial and fragile: isolate it, fail soft, never block the pipeline), `YouTubeLiveChatReplay`.
- `Transcriber` — audio → word-level transcript. Adapter: `FasterWhisperTranscriber`.
- `SignalExtractor` — produces a time-series signal (see 4.3). Each signal is its own plugin.
- `Scorer` — combines signals → candidate windows with scores.
- `LLMJudge` — rates/justifies candidates from transcript text. Adapter: `AnthropicJudge`. Must have a `NullJudge` so the pipeline works with no LLM.
- `Renderer` — cut + reframe + caption → MP4. Adapter: `FfmpegRenderer`.
- `Publisher` — post a clip somewhere. Adapters: `ManualDownloadPublisher` (MVP), `YouTubePublisher`, `InstagramPublisher` (later, flagged).
- `BlobStore`, `JobQueue`, `Clock`, `EventBus` (in-process first; swappable for Redis streams later).

Registry/config selects adapters. No adapter is imported directly by domain code.

### 4.2 Modules

```
clipforge/
  apps/
    api/            # FastAPI routers, DTOs, auth seam
    worker/         # job handlers (thin: call services)
    web/            # React UI
  clipforge/
    domain/         # entities, value objects, events (pure Python, no I/O)
    services/       # use cases: IngestSource, AnalyzeVideo, RenderClip, PublishClip
    ports/          # interfaces listed above
    adapters/       # concrete implementations
    signals/        # one file per signal extractor
    scoring/        # combiner, windowing, boundary snapping
    compliance/     # allowlist checks, credit, takedown
    infra/          # db, queue, storage, config, logging, metrics
  migrations/
  tests/            # unit, integration, e2e fixtures with tiny sample videos
  docs/
    ARCHITECTURE.md
    DECISIONS.md
    SAAS_READINESS.md
    PROMPTS.md      # all LLM prompts, versioned
  docker-compose.yml
```

### 4.3 The pipeline (each stage is an idempotent, resumable job)

`ingest → fetch → transcribe → extract signals → generate candidates → judge → render → review → publish`

State lives in the DB (`jobs`, `job_runs` with stage, status, attempts, error, started/finished). Re-running a stage with the same inputs must reuse cached artifacts (hash of input + config version). A failure in one candidate or one signal must not fail the whole video.

**Signals** (each returns a normalized 0–1 series at 1 s resolution plus metadata; all optional except transcript):
1. `chat_velocity` — messages/sec relative to a rolling baseline, spike detection (strongest signal for Twitch/Kick/YT live replay).
2. `emote_burst` — share of emote/spam tokens (e.g. W, LUL, KEKW, 😂, repeated chars) in chat.
3. `audio_energy` — loudness spikes, sudden volume changes, laughter-like bursts (start with RMS/LUFS deltas; leave a hook for a laughter classifier).
4. `speech_intensity` — words per second, exclamations, raised-voice proxy from transcript timing.
5. `llm_moment_score` — LLM rates transcript windows for funny / shocking / clutch / emotional / quotable, plus whether the window has a clear setup and payoff.
6. `visual_motion` — scene/motion change (cheap ffmpeg scene score first; optional later).
7. `keyword_boost` — configurable per-game / per-event keywords (e.g. GTA 6 terms), config-driven.

**Candidate generation**
- Smooth and combine signals with configurable weights (`scoring_profile`, stored in DB and versioned). Peak-pick, then expand each peak to a window of 20–60 s.
- Snap start/end to sentence or pause boundaries from the transcript; add 1–2 s lead-in and a short tail.
- Non-max suppression so overlapping windows collapse. Enforce a min gap and a max clips-per-hour limit.
- Only send the top N windows (default 30 per hour of video) to the `LLMJudge`, never the full transcript, to control cost.
- Final score = weighted combination + judge score. Store every component score so I can see why a clip ranked where it did.
- Output per candidate: `start`, `end`, `score`, `components{}`, `title_suggestions[3]`, `hook_text`, `reason`.

**Render**
- Layouts: `facecam_top_gameplay_bottom`, `center_crop`, `gameplay_blur_background`. Auto-detect facecam; allow manual crop override saved per source.
- Captions: word-by-word highlighted, generated as ASS from Whisper word timestamps, burned in with ffmpeg. Style presets in config (font, size, colors, position, safe areas for each platform's UI).
- Loudness normalization, 1080×1920, H.264, `-preset veryfast` for previews and `slow` for final export. Optional watermark/credit lower-third.
- Previews first (low-res, fast), final render only on approval.

### 4.4 Data model (SaaS-ready from day one)

Every business table has `workspace_id` (UUID, FK). In the MVP there is exactly one workspace and one user, created by a seed script. All queries go through a repository layer that **requires** a workspace context; there are no unscoped queries.

Tables (minimum):
`workspaces`, `users`, `memberships`, `sources` (allowlist), `videos`, `video_assets` (audio, proxy, chat dump, transcript), `transcripts`, `signal_series`, `candidates`, `clips`, `clip_renders`, `publish_targets`, `publications`, `jobs`, `job_runs`, `scoring_profiles`, `caption_styles`, `feedback_events`, `usage_events`, `audit_log`.

Notes:
- `usage_events` records every billable-ish action (minutes transcribed, LLM tokens, render seconds, GB stored) even though nothing is billed now. This is the future metering table.
- `feedback_events` records my approve/reject/trim/edit decisions and later real-world performance (views, watch time) per published clip. This is the training/tuning dataset. Capture it from Milestone 4 onward.
- Use UUIDv7 or ULID ids. Soft delete where it matters. All timestamps UTC.
- Alembic migrations only; no manual schema changes.

### 4.5 API

REST under `/api/v1`, OpenAPI generated. MVP endpoints:
- `POST /videos` (url, optional scoring_profile, optional override flag) → creates ingest job
- `GET /videos`, `GET /videos/{id}` (status per stage, progress)
- `GET /videos/{id}/candidates` (ranked, with component scores)
- `PATCH /candidates/{id}` (approve, reject, edit start/end, edit caption text, choose layout/style)
- `POST /clips/{id}/render`, `GET /clips/{id}/download`
- `GET/POST/PATCH /sources` (allowlist management)
- `GET/POST /scoring-profiles`, `GET/POST /caption-styles`
- `POST /clips/{id}/publish` (target) — returns `501` until Milestone 6
- `GET /health`, `GET /metrics`

Auth: a single static API token via env var in MVP, implemented as a `current_principal` dependency that returns `(user, workspace)`. Swapping to real auth later must only touch that dependency.

### 4.6 Config and secrets

12-factor. Pydantic Settings. `.env.example` committed, real secrets never. Feature flags in config: `ENABLE_LIVE`, `ENABLE_AUTOPUBLISH`, `ENABLE_LLM_JUDGE`, `ENABLE_KICK_CHAT`. Every external call has timeouts, retries with backoff, and a circuit breaker for the unofficial Kick adapter.

### 4.7 Observability

Structured JSON logs with `video_id`, `job_id`, `workspace_id` on every line. Prometheus metrics (stage durations, queue depth, failures, LLM tokens, render time). Per-video cost report: GPU seconds, LLM tokens/cost, storage. This is how I'll know if it's profitable.

### 4.8 Storage and cost control

- Download audio first for transcription; fetch full video only for candidate windows when possible (range download), otherwise keep a low-res proxy for analysis.
- Retention policy: raw video auto-deleted N days after processing (config); approved clips kept. Never grow disk unbounded.
- Cache transcripts and signals by content hash.

## 5. Milestones (build in order; each ends with a demo and tests)

**M0 — Skeleton.** Repo, Docker Compose, config, logging, DB + migrations, health endpoint, CI (lint, type-check, tests), seed script for the single workspace/user, `docs/` stubs.

**M1 — Ingest + transcript.** Resolve URL, enforce source allowlist, download audio, transcribe with word timestamps, store transcript. CLI: `clipforge ingest <url>`. Works on a YouTube VOD and a Twitch VOD.

**M2 — Signals + candidates (no LLM).** Chat replay (Twitch + YouTube first), chat velocity, emote burst, audio energy, speech intensity. Combiner, windowing, boundary snapping, NMS. CLI prints ranked candidates with component scores. Include a **replay harness**: given a video and a hand-labelled list of "good moment" timestamps (`tests/fixtures/labels.json`), report precision@K and recall@K so scoring changes can be evaluated objectively.

**M3 — LLM judge.** `AnthropicJudge` + `NullJudge`, prompts in `docs/PROMPTS.md`, structured JSON output with schema validation, retries, token accounting, caching. Titles and hook suggestions.

**M4 — Render.** Cut, reframe layouts, ASS word-by-word captions, preview and final renders. Start recording `feedback_events`.

**M5 — Review UI.** React app: add URL, progress view, candidate list with video preview, approve/reject/trim/caption edit, download, source allowlist screen, scoring profile selector. Mobile-friendly (I review on my phone).

**M6 — Publishing.** `Publisher` port with `YouTubePublisher` (Data API, resumable upload; assume private/unlisted until the API project is audited) and a scheduling queue. Instagram/TikTok adapters are stubs behind the flag. Always require explicit approval per clip unless `ENABLE_AUTOPUBLISH` is on and the source's policy permits it. Pull back view stats into `feedback_events`.

**M7 — Live mode.** `StreamlinkLiveFetcher` records rolling chunks; the pipeline runs on a sliding window; candidates appear in the UI within minutes of the moment. Kick/Twitch live chat via adapters. Hard limits on disk and GPU.

**M8 — Tuning loop.** Use `feedback_events` to fit scoring weights (start with logistic regression on component scores vs. approved/performed). Per-game and per-streamer scoring profiles. A/B compare profiles using the replay harness.

## 6. SaaS-readiness checklist (design for it now, build it later)

Document status of each in `docs/SAAS_READINESS.md` and keep it current.

- [ ] Multi-tenancy: `workspace_id` everywhere, repository layer enforces it, tests prove no cross-tenant reads. (Add Postgres row-level security as a later hardening step.)
- [ ] AuthN/AuthZ seam: `current_principal` dependency, roles in `memberships` (owner/editor/viewer).
- [ ] Metering: `usage_events` complete and queryable per workspace per day.
- [ ] Quotas and rate limits: a `QuotaService` interface that currently always allows.
- [ ] Billing: nothing implemented; `plan` and `subscription` tables are *not* created yet, but `usage_events` is the contract a billing integration will read.
- [ ] Job isolation: queues are per-priority; per-workspace concurrency limits are a config stub. GPU workers are separate processes that can scale horizontally.
- [ ] Storage: all paths are keys in a `BlobStore` with a workspace prefix; S3 adapter swap is a config change.
- [ ] Secrets per workspace: OAuth tokens for publishing stored encrypted (envelope encryption interface), never in plaintext columns.
- [ ] Compliance: audit log, takedown flow, per-workspace source policies, ToS/DMCA process documented.
- [ ] Observability: per-workspace cost attribution.
- [ ] Config/versioning: scoring profiles and prompts are versioned, so results are reproducible.
- [ ] API: versioned (`/api/v1`), pagination and idempotency keys on mutating endpoints.

## 7. Quality bar

- Type-hint everything; `mypy --strict` on `domain/`, `services/`, `ports/`.
- Unit tests for scoring, windowing, boundary snapping, caption timing, allowlist enforcement. Integration tests with a tiny (≈60 s) fixture video and fake adapters, no network in CI.
- Every adapter has a contract test shared with its fake.
- Idempotency test: running any stage twice produces no duplicates.
- Failure-injection tests: Kick chat down, Whisper OOM, ffmpeg crash, LLM timeout. The pipeline must degrade gracefully (e.g. fall back to audio + transcript signals, `NullJudge`).
- No secrets in logs. No unbounded subprocess output. All `ffmpeg`/`yt-dlp` calls have timeouts and are run with argument lists (no shell strings).

## 8. First deliverables right now

1. A written plan (repo layout, schema DDL, port interfaces as Python Protocols, milestone breakdown with estimates).
2. A list of the open questions you need me to answer (GPU availability, Python vs. other preferences, which platforms matter first, hosting target).
3. Wait for my approval, then start **M0**.

Keep answers concise. Surface risks and trade-offs early. If something in this spec is wrong or risky, say so instead of silently working around it.
