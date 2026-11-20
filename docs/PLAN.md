# ClipForge — Implementation Plan (awaiting approval)

Source spec: `CLAUDE CODE PROMPT.md` (repo root).

## 1. Repo layout

```
wide2vertical/                     # git root (GitHub: Aniket-89/wide2vertical)
  pyproject.toml                   # uv workspace, ruff, mypy, pytest config
  uv.lock
  .env.example  .pre-commit-config.yaml  .github/workflows/ci.yml
  docker-compose.yml               # api, worker, worker-gpu (profile), postgres, redis, web
  docker/Dockerfile.python         # one image for api + worker (ffmpeg, yt-dlp baked in)
  docker/Dockerfile.web
  apps/
    api/        main.py, deps.py (current_principal), routers/, dto/
    worker/     main.py (Dramatiq entry), handlers/ (one per pipeline stage, thin)
    cli/        main.py (Typer: `clipforge ingest|analyze|candidates|seed|eval`)
    web/        React + Vite + TS
  clipforge/
    domain/     entities.py, values.py (TimeRange, Score, Platform…), events.py   # pure, no I/O
    ports/      one module per Protocol (see §3)
    services/   ingest.py, analyze.py, candidates.py, judge.py, render.py, publish.py
    adapters/   resolvers/, fetchers/, chat/, transcribers/, judges/, renderers/,
                publishers/, blobstore/, queue/, eventbus/   (+ fakes/ for tests)
    signals/    chat_velocity.py, emote_burst.py, audio_energy.py, speech_intensity.py,
                llm_moment.py, visual_motion.py, keyword_boost.py
    scoring/    combine.py, peaks.py, windows.py, snapping.py, nms.py
    compliance/ allowlist.py, credit.py, takedown.py
    infra/      config.py (Pydantic Settings), db/ (models, session, repositories),
                registry.py (adapter wiring), logging.py, metrics.py, retry.py,
                circuit_breaker.py, hashing.py
  migrations/                      # Alembic
  tests/        unit/, contract/, integration/, e2e/, fixtures/ (60 s sample video, chat, labels.json)
  docs/         ARCHITECTURE.md, DECISIONS.md, SAAS_READINESS.md, PROMPTS.md, PLAN.md
```

**Import rules** (checked in CI with `import-linter`): `domain` imports nothing internal;
`ports` → `domain`; `services`/`signals`/`scoring`/`compliance` → `ports`+`domain`;
only `infra/registry.py` imports `adapters`; `apps` → `services`+`infra`.

## 2. Schema (DDL sketch — Alembic will own the real thing)

Conventions: `id UUID PRIMARY KEY` (UUIDv7, app-generated), `workspace_id UUID NOT NULL REFERENCES workspaces`
on every business table, `created_at/updated_at TIMESTAMPTZ NOT NULL DEFAULT now()`,
`deleted_at TIMESTAMPTZ NULL` where soft delete matters (sources, videos, clips).

```sql
workspaces(id, name, slug UNIQUE, created_at)
users(id, email UNIQUE, display_name, created_at)
memberships(id, workspace_id, user_id, role TEXT CHECK (role IN ('owner','editor','viewer')), UNIQUE(workspace_id,user_id))

sources(id, workspace_id, platform TEXT, channel_handle TEXT, channel_id TEXT NULL,
        clip_policy TEXT CHECK (clip_policy IN ('campaign','written_permission','open_policy','own_channel','unknown')),
        policy_url, required_credit_text, notes, status TEXT CHECK (status IN ('active','retired')),
        retired_at, deleted_at, UNIQUE(workspace_id, platform, channel_handle))

videos(id, workspace_id, source_id NULL, url, platform, external_id, title, duration_s,
       recorded_at, override_used BOOL, scoring_profile_id, status, deleted_at,
       UNIQUE(workspace_id, platform, external_id))
video_assets(id, workspace_id, video_id, kind TEXT  -- 'audio','proxy','full','chat_dump','window'
             , blob_key, content_hash, bytes, expires_at NULL)
transcripts(id, workspace_id, video_id, model, language, content_hash, config_version,
            words JSONB  -- [{w,start,end,p}]
            , UNIQUE(video_id, content_hash, config_version))
signal_series(id, workspace_id, video_id, signal_name, signal_version, resolution_s,
              values REAL[], meta JSONB, input_hash, UNIQUE(video_id, signal_name, input_hash))

scoring_profiles(id, workspace_id, name, version INT, weights JSONB, params JSONB,
                 is_default BOOL, UNIQUE(workspace_id,name,version))   -- immutable rows; new version = new row
caption_styles(id, workspace_id, name, version INT, spec JSONB)

candidates(id, workspace_id, video_id, scoring_profile_id, start_s, end_s, score,
           components JSONB, judge JSONB NULL, title_suggestions TEXT[], hook_text, reason,
           rank INT, status TEXT CHECK (status IN ('pending','approved','rejected')),
           UNIQUE(video_id, scoring_profile_id, start_s, end_s))
clips(id, workspace_id, candidate_id UNIQUE, start_s, end_s, caption_override JSONB,
      layout TEXT, caption_style_id, crop_override JSONB, commentary_text, credit_text, deleted_at)
clip_renders(id, workspace_id, clip_id, kind TEXT CHECK (kind IN ('preview','final')),
             render_hash, blob_key, status, duration_s, render_seconds, UNIQUE(clip_id, kind, render_hash))

publish_targets(id, workspace_id, platform, account_label, secret_ref  -- pointer to encrypted secret, never the token
                , enabled)
publications(id, workspace_id, clip_id, publish_target_id, status, external_id, scheduled_at,
             published_at, idempotency_key UNIQUE)

jobs(id, workspace_id, video_id NULL, kind, status, input_hash, priority, created_at)
job_runs(id, workspace_id, job_id, stage, status, attempt INT, error TEXT, started_at, finished_at)

feedback_events(id, workspace_id, candidate_id NULL, clip_id NULL, kind, payload JSONB, occurred_at)
usage_events(id, workspace_id, kind  -- 'transcribe_minutes','llm_tokens_in','llm_tokens_out','render_seconds','storage_gb_day','gpu_seconds'
             , quantity NUMERIC, unit, video_id NULL, meta JSONB, occurred_at)
audit_log(id, workspace_id, actor_user_id NULL, action, target_type, target_id, details JSONB, occurred_at)
idempotency_keys(workspace_id, key, request_hash, response JSONB, created_at, PRIMARY KEY(workspace_id,key))
```

Repositories take a `WorkspaceScope` in their constructor; there is no API that queries without one.
A test enumerates all repository methods across two workspaces and asserts zero cross-reads.

## 3. Port interfaces (Python Protocols)

```python
class SourceResolver(Protocol):
    def supports(self, url: str) -> bool: ...
    def resolve(self, url: str) -> ResolvedVideo: ...          # platform, channel_handle, external_id, title, duration

class MediaFetcher(Protocol):
    def fetch_audio(self, video: ResolvedVideo, dest: BlobKey) -> Asset: ...
    def fetch_proxy(self, video: ResolvedVideo, dest: BlobKey, height: int = 360) -> Asset: ...
    def fetch_window(self, video: ResolvedVideo, rng: TimeRange, dest: BlobKey) -> Asset: ...

class ChatProvider(Protocol):
    platform: Platform
    def fetch_replay(self, video: ResolvedVideo) -> Iterator[ChatMessage]: ...   # t_s, author_hash, text, emotes

class Transcriber(Protocol):
    def transcribe(self, audio: LocalPath, language: str | None = None) -> Transcript: ...  # word-level

class SignalExtractor(Protocol):
    name: str; version: str; required: bool
    def extract(self, ctx: SignalContext) -> SignalSeries | None: ...  # 0–1 at 1 s; None = unavailable

class Scorer(Protocol):
    def candidates(self, signals: Mapping[str, SignalSeries], transcript: Transcript,
                   profile: ScoringProfile) -> list[CandidateWindow]: ...

class LLMJudge(Protocol):
    def judge(self, windows: Sequence[JudgeInput]) -> list[JudgeVerdict]: ...   # scores, titles[3], hook, reason, usage

class Renderer(Protocol):
    def render(self, spec: RenderSpec) -> RenderResult: ...    # source asset, range, layout, crop, captions(ASS), quality

class Publisher(Protocol):
    platform: str
    def publish(self, clip: PublishableClip, target: PublishTarget, idem_key: str) -> PublishResult: ...

class BlobStore(Protocol):
    def put(self, key: BlobKey, src: LocalPath) -> None: ...
    def open_local(self, key: BlobKey) -> ContextManager[LocalPath]: ...
    def delete(self, key: BlobKey) -> None: ...
    def url(self, key: BlobKey, ttl_s: int = 3600) -> str: ...

class JobQueue(Protocol):
    def enqueue(self, kind: str, payload: Mapping[str, Any], *, priority: Priority, delay_s: int = 0) -> JobId: ...

class Clock(Protocol):    def now(self) -> datetime: ...
class EventBus(Protocol): def publish(self, e: DomainEvent) -> None: ...; def subscribe(self, t: type[E], h: Callable[[E], None]) -> None: ...
class QuotaService(Protocol): def check(self, ws: WorkspaceId, kind: str, qty: float) -> QuotaDecision: ...  # always allow in MVP
class SecretBox(Protocol): def seal(self, ws: WorkspaceId, plaintext: bytes) -> SecretRef: ...; def open(self, ref: SecretRef) -> bytes: ...
```

`BlobKey` is always `ws/{workspace_id}/...` and is built only by a helper.

## 4. Milestones and estimates (focused working days, solo + Claude)

| # | Scope | Est. |
|---|---|---|
| M0 | uv project, Compose, Settings, JSON logging, SQLAlchemy models + first migration (all tables), seed script, `/health`, `/metrics`, `current_principal`, CI (ruff, mypy strict on core, pytest, import-linter), docs stubs | 2–3 d |
| M1 | Resolvers (YT/Twitch/Kick), allowlist + override audit, `YtDlpFetcher` audio, `FasterWhisperTranscriber`, content-hash caching, job/job_run tracking, `clipforge ingest` | 3–4 d |
| M2 | Chat replay (Twitch, YT), 4 signals, combiner/peaks/windows/snapping/NMS, CLI ranking, replay harness (P@K, R@K) | 4–5 d |
| M3 | AnthropicJudge + NullJudge, prompts in PROMPTS.md, JSON schema validation, retries, token → `usage_events`, cache | 2 d |
| M4 | FfmpegRenderer, 3 layouts, facecam detection, ASS word captions, preview/final, `feedback_events` | 4–5 d |
| M5 | React review UI, mobile-first | 4–5 d |
| M6 | YouTubePublisher, scheduling, stats pullback | 3–4 d |
| M7 | Live mode | 5–7 d |
| M8 | Tuning loop | 2–3 d |

## 5. Key decisions I'm proposing (will become ADRs)

1. **Dramatiq over ARQ.** Pipeline stages are CPU/GPU-bound and sync (ffmpeg, whisper); Dramatiq's sync actors, retries, and priority queues fit better, and it's more actively maintained. Wrapped in `JobQueue`.
2. **One Python image, two worker pools:** `worker` (CPU: fetch, signals, render) and `worker-gpu` (transcribe) on separate queues, so GPU scales independently.
3. **Sync SQLAlchemy** (not async) for workers + API; simpler and FastAPI handles it fine at this scale.
4. **UUIDv7 via `uuid-utils`** (Python 3.12 lacks `uuid.uuid7`).
5. **Signals are plain functions over numpy arrays**, with no pandas dependency.
6. **Plain Postgres, no TimescaleDB** in MVP. Signal series are small (one row per signal per video as `REAL[]`).

## 6. Risks / spec issues to flag

- **Twitch VOD chat replay has no official API.** The only way is Twitch's internal GQL endpoint (what tools like TwitchDownloader use). That's unofficial and arguably in tension with §2 "respect ToS". I propose: implement it behind `ENABLE_TWITCH_CHAT_GQL` (default off), fail soft, and document the risk. Same story for **Kick** (unofficial and Cloudflare-protected) and **YouTube live-chat replay** (yt-dlp extracts it, also unofficial).
- **yt-dlp vs YouTube ToS.** YouTube's ToS forbids downloading outside its own features, even with the creator's permission. For `own_channel` sources, YouTube Studio download is the clean path. I'll keep yt-dlp as the adapter (spec says so) but record this in DECISIONS.md.
- **Windows host + GPU in Docker** needs WSL2 + NVIDIA Container Toolkit. CPU fallback (int8 `small`/`medium` model) will be the default until GPU is confirmed.
- **"Range download" of windows** works for YouTube (DASH) via `--download-sections`, but is unreliable on Twitch HLS VODs. Fallback: a 360p proxy for analysis + window fetch at render time.
- **libx264 is GPL.** That's fine server-side, even for SaaS. Noted only for completeness.
- **Scope:** M0–M6 ≈ 4–5 weeks of focused work. M7 (live) is the riskiest and should be re-evaluated after M5.
