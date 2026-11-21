# Decisions (ADR log)

Short entries, newest last. Status: Accepted unless noted.

## ADR-0001 — Dramatiq over ARQ for the job queue (2026-11-20)
Pipeline stages are sync and CPU/GPU-bound (ffmpeg, whisper). Dramatiq has sync actors,
built-in retries with backoff, and is actively maintained; ARQ is asyncio-first.
Dramatiq is hidden behind the `JobQueue` port, and a single dispatch actor keeps it out of
handlers entirely.

## ADR-0002 — Priorities as separate queues (2026-11-20)
The Redis broker ignores per-message priority. Each priority level is its own queue
(`default_high`, `default`, `default_low`); workers subscribe to all three. That also lines
up with SaaS-readiness "queues are per-priority".

## ADR-0003 — Sync SQLAlchemy, TEXT+CHECK instead of PG enums (2026-11-20)
Sync sessions are simpler and fine at this scale (workers are sync anyway). TEXT+CHECK
constraints are easier to evolve in migrations than Postgres enum types.

## ADR-0004 — UUIDv7 via `uuid-utils` (2026-11-20)
Python 3.12's stdlib has no `uuid7`. UUIDv7 gives time-ordered PKs (index-friendly, and
"order by id" ≈ "order by creation").

## ADR-0005 — CPU-first transcription (2026-11-20)
The host is Windows. GPU-in-Docker needs WSL2 plus the NVIDIA Container Toolkit, which isn't set
up yet. Default: `faster-whisper` `small`, int8, CPU. The `worker-gpu` Compose profile exists
and consumes the `transcribe*` queues; switching to CUDA means changing config and the base image,
not code.

## ADR-0006 — Unofficial chat-replay adapters are flagged off by default (2026-11-20)
Twitch VOD chat (internal GQL), Kick (unofficial API, Cloudflare), and YouTube live-chat replay
(yt-dlp extraction) have no official API. That's in tension with spec §2 ("respect ToS").
Decision (owner-approved): build them behind `ENABLE_TWITCH_CHAT_GQL`, `ENABLE_KICK_CHAT`,
`ENABLE_YOUTUBE_CHAT_REPLAY` (all default false), isolated, fail-soft, never blocking the
pipeline. Scoring must work on audio + transcript alone.

## ADR-0007 — `127.0.0.1` instead of `localhost` for local service URLs (2026-11-20)
On this Windows host, `localhost` resolves to `::1` first, and Docker Desktop's IPv6 port
forward stalls connections indefinitely. IPv4 loopback is used in all local defaults.

## ADR-0008 — yt-dlp as the default fetcher, with a ToS caveat (2026-11-20)
YouTube's ToS disallows downloading outside YouTube's own features, even with creator
permission. yt-dlp stays the default `MediaFetcher` (per spec), but for `own_channel` sources the
clean path is a manual YouTube Studio download, to be supported later by a `LocalFileFetcher`.

## ADR-0009 — Redis published on host port 6380 (2026-11-20)
A native Windows Redis 5.0 already listens on 127.0.0.1:6379 on the dev host, and it is too old
for redis-py's RESP3 handshake. The Compose Redis is published on 6380; containers still use
`redis:6379` internally.

## ADR-0010 — Allowlist policy details (2026-11-20)
`compliance.allowlist.decide` (pure, table-tested):
- **Retired source → always refused, even with `--override`.** A takedown is a promise to the
  creator, so nothing in the tool can bypass it. To undo, un-retire the source deliberately.
- Not in the allowlist, or policy `unknown` → refused unless override (then processed, flagged
  `videos.override_used`, and audit-logged as `ingest.override`).
- `campaign` / `written_permission` without `required_credit_text` → refused unless override.
  Those policies almost always mandate credit, so a missing credit is a setup error.
- Every refusal is audit-logged as `ingest.refused`.

## ADR-0011 — Persistence port for services (2026-11-20)
Services may not import `infra`, so `IngestPipeline` talks to a `PipelineStore` Protocol
(`ports/pipeline.py`). `SqlPipelineStore` implements it, and an `InMemoryPipelineStore` fake
must pass the same contract tests (`tests/contract`).

## ADR-0012 — Stage failures are committed, not rolled back (2026-11-20)
Each stage writes its output as its last step, so if it raises, the only pending writes are
the `job_runs` failure row and audit entries. CLI and worker use
`session_scope(commit_on_error=True)` so failures stay visible in `GET /videos/{id}`.

## ADR-0013 — `POST /videos` resolves synchronously, queues the heavy work (2026-11-20)
Resolve + allowlist take ~1–3 s (one `yt-dlp -J`), so refusals come back immediately as 403
with the reason. Fetch and transcription are queued (`pipeline.fetch` → `pipeline.transcribe`).
Expected `PipelineError`s are recorded and not retried; unexpected exceptions use Dramatiq
retries (3, exponential backoff).

## ADR-0014 — Pin PyAV < 16 (2026-11-20)
faster-whisper 1.2.1 calls `av.open(..., metadata_errors=...)`, which PyAV ≥ 16 removed. The real
transcriber contract test caught it. Remove the pin when faster-whisper releases a fix.

## ADR-0015 — Fetch audio only; full-video fetching deferred to M4 (2026-11-20)
M1 downloads `bestaudio` only (spec §4.8). M4 will add window/proxy fetching to
`MediaFetcher`; for Twitch HLS, range fetching is unreliable and a low-res proxy is the
fallback.

## ADR-0016 — Twitch HLS is downloaded with ffmpeg (2026-11-20)
yt-dlp's native HLS downloader fails on Twitch VOD fMP4 playlists ("Initialization fragment
found after media fragments"). When ffmpeg is on PATH (always, in the Docker image),
`YtDlpFetcher` passes `--downloader m3u8:ffmpeg`. Found during the real M1 Twitch run.

## ADR-0017 — Worker declares every queue; producers never install a broker (2026-11-20)
A Dramatiq worker consumes only *declared* queues, and the single dispatch actor declares only
`default`. `build_worker_broker` therefore declares every priority variant of every queue base
(`QUEUE_BASES`). Producer brokers (API, CLI, and the worker enqueueing follow-up jobs) are
created with `install=False`, so they never replace the worker's global broker. Found during the
real M1 Twitch run: the transcribe job sat in Redis unconsumed.

## ADR-0018 — Fetched media is length-verified (2026-11-20)
During the real Twitch run, ffmpeg's HLS demuxer stopped after 685 s of a 3,024 s VOD (a
transient network error) and still exited 0. The pipeline stored the truncated file and called
it a success. Now:
- `YtDlpFetcher` measures the file with ffprobe and retries (up to `retries`) when it is under
  `MIN_COMPLETENESS` (98%) of the platform-reported duration, then fails.
- `IngestPipeline.fetch_audio` independently refuses to store a short file, whichever fetcher
  produced it.
Known gap: `video_assets` doesn't store duration, so an asset cached *before* this guard isn't
re-verified. Add a `duration_s` column at the next migration.

## ADR-0019 — ffmpeg HLS with keepalive off; chunked fetching rejected (2026-11-20)
Root cause of the truncation in ADR-0018: TLS decryption errors on the dev network made
ffmpeg's HLS keepalive reconnect end the stream early (exit 0). Passing
`-http_persistent 0` (a fresh connection per segment) gave complete downloads on every
test run, where 4 of 5 attempts had truncated before. The length check and whole-file retry from
ADR-0018 stay as the safety net.
Rejected: chunked `--download-sections` fetching. ffmpeg 5.1 mis-seeks in Twitch playlists
with mid-stream `EXT-X-MAP` changes and returns empty sections. If truncation recurs on
real hosts, the next step is a segment-level HLS downloader with per-segment retries
(Debian's ffmpeg 5.1 also lacks `seg_max_retry`, which arrived in 6.0).

## ADR-0020 — Canonical URLs and pinned yt-dlp extractors (2026-11-20)
Security review: Python's `urlparse` and yt-dlp can parse odd URLs differently (userinfo,
backslashes, ports). A URL that looks like YouTube to our allowlist check could reach another
host through yt-dlp's generic extractor (an allowlist bypass and SSRF). Now:
- `parse_video_url` rejects whitespace, control chars, backslashes, `@`, ports, credentials
  and over-long input.
- Downstream tools only ever see a canonical URL rebuilt from the parsed ID
  (`https://www.youtube.com/watch?v=<id>`, `https://www.twitch.tv/videos/<id>`,
  `https://kick.com/<slug>/videos/<id>`). `ResolvedVideo.url` is that canonical URL, never
  the raw input or yt-dlp's `webpage_url`.
- Every yt-dlp call passes `--use-extractors youtube|twitch:vod|kick:vod` and `--` before
  the URL, so the generic extractor can't be reached.
- `kick.com/video/<id>` is no longer accepted (yt-dlp's `kick:vod` requires the channel slug).

## ADR-0021 — Takedowns apply to in-flight work and override-ingested videos (2026-11-20)
Security review found two gaps. (1) A source retired after ingest still had its queued fetch
and transcribe run. (2) `retire()` matched videos only by `source_id`, so videos ingested via
`--override` before the channel was allowlisted escaped the takedown. Now:
- `videos.channel_handle` / `channel_id` are stored at ingest (migration `102c4b9c91eb`).
- `fetch_audio` and `transcribe` re-check the source via `PipelineStore.source_for_video`
  (by `source_id`, else by stored channel). A retired source stops the stage, audit-logs
  `pipeline.blocked`, and sets the video to `source_retired`.
- `SourceAdmin.add` backfills `source_id` on matching override videos. `retire()` matches by
  `source_id` *or* platform+channel, marks those videos `source_retired`, and hides their
  clips. Stored audio and transcripts are kept (the spec says hide), not deleted.

## ADR-0018 addendum (2026-11-20)
`video_assets.duration_s` now stores the measured length, and a cached asset that measured
short is re-fetched. Unknown duration (no ffprobe on the host) is trusted, so runs don't
re-download every time.

## ADR-0022 — Chat authors are stored as salted hashes (2026-11-20)
Chat authors are third parties who never agreed to be in our dataset. Adapters store
`sha256(workspace_id + lowercase name)[:16]`. That's enough to count unique chatters and spot
spam, not reversible to a username, and different per workspace. Message text is kept, because the
signals need it.

## ADR-0023 — Signal and scoring design (2026-11-20)
- **Local baselines, not global.** Each signal is scored against a rolling median/MAD
  (chat 300 s, audio 60 s, speech 120 s), so a stream that gets busier over time doesn't drown
  early moments, and quiet streams still register.
- **Soft saturation** `z / (z + z_half)` instead of clipping. Hard clipping made a 4σ noise blip
  and a 29σ chat explosion both 1.0, and the synthetic-scenario test caught noise outranking a
  real moment.
- **Chat lag correction** (`chat_lag_s`, default 6). Chat reacts after the moment, so chat
  signals are shifted earlier. A test proves that without it, peaks land late.
- **Weights re-normalise over available signals.** No chat → audio + speech carry the score
  instead of being capped at (1 − chat weight).
- **Windows:** peak − `setup_s` (18) to peak + `payoff_s` (9). Edges snap to the nearest sentence
  end or ≥0.7 s pause within ±6 s, never past the peak. Then lead-in 1.5 s / tail 1 s, and length
  is clamped to 20–60 s (the setup is trimmed first, the payoff kept). NMS uses IoU 0.3, a 30 s
  peak gap and `max_clips_per_hour`.
- **Window score** = 0.6 × combined peak + 0.4 × combined mean. Every signal's in-window max is
  stored as `components`, plus a human-readable `reason`.
- **Every parameter** lives in the scoring profile's `params`/`weights` and feeds the signal
  cache key, so changing a profile recomputes exactly what it affects.

## ADR-0024 — Twitch VOD chat: integrity check stops us; official live capture instead (2026-11-20)
On the real M2 run, Twitch's GQL endpoint answered "failed integrity check". VOD comments now
require a client-integrity token (anti-bot). Obtaining one would mean evading bot protection,
which spec §2 forbids, so we stop there: the adapter fails soft and analysis continues on audio
+ transcript (shown on the real VOD). The adapter stays behind its flag in case Twitch relaxes
this.
**Path forward:** capture chat *live* through Twitch's official chat interface (IRC/EventSub)
in M7 and store it as the `chat_dump` asset. VODs recorded while capture runs then get chat
signals with no unofficial endpoints. YouTube live-chat replay (yt-dlp) is implemented and
fixture-tested but not yet verified on a real past livestream.

## ADR-0025 — URL rules live in the domain (2026-11-20)
`parse_video_url` and canonicalisation are pure rules the CLI needs (to map labels to videos),
so they moved to `clipforge.domain.urls`. The yt-dlp extractor map stays in the adapter.
import-linter caught the CLI importing the adapter directly.

## ADR-0026 — Re-analysis replaces only pending candidates (2026-11-20)
`replace_candidates` deletes this profile's *pending* candidates (never ones with clips) and
inserts the new ranking, skipping windows identical to reviewed ones. Approve/reject decisions
are training data (`feedback_events` from M4) and must survive re-scoring. The replay harness
(`clipforge eval`) runs with `persist=False` and never touches candidates.

## ADR-0027 — LLM judge: cheap model, structured outputs, bounded influence (2026-11-21)
- **Model:** `claude-haiku-5-5` (the spec asks for a cheap scoring model; $0.10 / $0.50 per
  MTok). Configurable via `CLIPFORGE_LLM_MODEL`.
- **Request:** `output_config.effort: "low"` (short classification work) plus
  `output_config.format` with a strict JSON schema; Pydantic then enforces ranges the schema
  can't express. No `temperature` (rejected by current models), no assistant prefill (400). The
  system prompt is frozen and marked cacheable.
- **Failures** (timeout, 429 after the SDK's retries, refusal, `max_tokens`, invalid output)
  become `JudgeUnavailable` *per batch*. Those windows keep their signal score. If every batch
  fails, the `judge` stage is recorded FAILED (video status `judge_failed`) but the candidates
  stay usable, so the pipeline degrades and doesn't stop.
- **Blending:** `final = (1 − w)·signal + w·overall`, with `w = llm_moment_score / (that +
  raw weights of the signals actually used)` (≈0.21 by default, ≈0.36 without chat). The judge
  re-orders; it can't fabricate a candidate the signals never proposed.
- **Off by default:** `AnthropicJudge` is built only when `ENABLE_LLM_JUDGE=true` *and* a key
  is set (paid service, spec §0.6); otherwise `NullJudge`.

## ADR-0028 — Judge cost control (2026-11-21)
Only the top `judge_top_n_per_hour` (profile param, default 30) pending windows per video
hour are judged, hard-capped at `JUDGE_MAX_WINDOWS_PER_VIDEO` (150), in batches of
`JUDGE_BATCH_SIZE` (8) per request. Each window sends only its own transcript (≤2,000 chars) plus
its signal components, never the full transcript. Verdicts are cached in the BlobStore under
`ws/<ws>/judge-cache/` keyed by sha256(prompt_id, model, transcript, duration, hints), so
re-analysis with unchanged windows costs $0. Every call writes `llm_tokens_in`,
`llm_tokens_out` (with cache read/write counts) and `llm_cost_usd_est` to `usage_events`.
Rough cost: a 1-hour VOD → 30 windows → 4 requests of ~3–4K input and ~1.5K output tokens,
≈ $0.005 per hour of video at Haiku 5.5 prices. The replay harness runs the judge only
with `--with-judge`.
The cache lives in the BlobStore rather than a new table because the data model is frozen
after M2 without owner approval.

## ADR-0029 — Prompts are versioned files with a fingerprint check (2026-11-21)
`clipforge/prompts/<id>.md` is the single source of truth; `docs/PROMPTS.md` indexes each id
with a 12-char content fingerprint, and a test fails if they drift apart. Released prompts are
never edited; a change means `…v2`. Each stored verdict records `prompt_id` and `model`.
Transcripts are treated as untrusted third-party data: they're sent as JSON values, the prompt
says to treat embedded instructions as speech, and the judge's influence is bounded (ADR-0027).

## ADR-0030 — Judge providers: Anthropic, plus OpenRouter / Groq via one OpenAI-compatible adapter (2026-11-21)
The owner may use OpenRouter or Groq. Both expose OpenAI-compatible Chat Completions, so one
`OpenAICompatJudge` (official `openai` SDK, Apache-2.0) covers them and any other compatible
endpoint (vLLM, Ollama) through `CLIPFORGE_LLM_BASE_URL`. `CLIPFORGE_LLM_PROVIDER` picks
`anthropic | openrouter | groq | openai_compat`. It uses the same versioned prompt, JSON schema
and Pydantic validation as `AnthropicJudge`, so verdicts look the same everywhere.
- **Structured output varies by model on these providers.** Strict `json_schema` is tried
  first; if the endpoint rejects `response_format`, the adapter switches once to `json_object`
  (with the schema spelled out in the system prompt and code fences stripped). Validation
  catches anything off-schema.
- `temperature: 0` here (allowed on these APIs, unlike current Claude models), for repeatable
  scores.
- `judge.model` is reported as `provider:model`, so provenance and the verdict cache stay
  separate per provider.
- `CLIPFORGE_LLM_MODEL` must be set explicitly for non-Anthropic providers (model names differ
  per provider); a misconfiguration gives `NullJudge` with the reason, never a crash.
- No OpenRouter attribution headers (`HTTP-Referer`/`X-Title`): we send nothing that
  identifies the user or project.
- Cost estimates use `CLIPFORGE_LLM_PRICE_*`, which default to Haiku 5.5 prices. Set them for
  the model you actually use, or the per-video cost report will be wrong.
- Prompt caching via `cache_control` is Anthropic-only; on other providers caching (if any) is
  automatic and reported through `prompt_tokens_details.cached_tokens`.

## ADR-0031 — Getting the clip's video: sections, HLS segments, ffmpeg versions (2026-11-21)
Spec §4.8: never download the whole VOD to render a clip.
- **YouTube:** `yt-dlp --download-sections --force-keyframes-at-cuts` (frame-accurate start,
  verified 12.0 s for a 12 s request).
- **Twitch/Kick (HLS):** ffmpeg 5.1 (Debian 12) *and* 7.1 (Debian 13) can't seek in Twitch
  playlists with mid-stream `EXT-X-MAP` changes. They streamed 166 MB from the start and output
  nothing; host ffmpeg 8 works. Pulling a third-party static ffmpeg 8 image was blocked by the
  environment's safety policy (unvetted binary), and a cleaner fix existed anyway:
  `HlsSegmentFetcher` parses the media playlist, picks only the segments overlapping the window
  from the EXTINF timeline, downloads them with retries (this network corrupts TLS sessions),
  and remuxes locally. The file's VOD start time is exact. Real run: 40 s of 720p in 5.6 s,
  16 MB.
- The image moved to `python:3.12-slim-trixie` (ffmpeg 7.1, `fonts-montserrat`). That also
  fixed a stray `\n` in the old Dockerfile's `mkdir` line.
- Sections are fetched with ±1 s padding and cached in the BlobStore with a JSON sidecar
  (start time, duration), so re-renders and small trims need no re-download.

## ADR-0032 — Layout selection and facecam detection (2026-11-21)
- OpenCV **4.x** Haar frontal-face cascade (pinned `<5`; OpenCV 5 dropped the bundled cascades,
  and YuNet needs a model download). Frames are decoded with PyAV, which is already a dependency.
- Auto rules (`rendering/layout.py`, pure): no stable face → `gameplay_blur_background` (never
  hides anything); a large face → `center_crop` following it; a small face → facecam layout
  **only if** it's stable (a webcam doesn't move), dominant (one face, or ≥2.5× the next) and
  near an edge/corner.
- The dominance and edge rules were added after a real false positive: a static wide shot of a
  stage panel (several small still faces) was stacked as a "facecam". Regression tests now
  cover that case. `LAYOUT_RULES_VERSION` is part of the render hash, so a rule change
  invalidates old renders.
- Manual overrides use normalised `[x, y, w, h]` rects (any resolution), per clip or per source
  (`sources.crop_override`).

## ADR-0033 — Render pipeline: preview vs final, idempotency (2026-11-21)
- Preview: 540×960, x264 veryfast CRF 28, from a ≤720p section; auto-rendered for the top
  `PREVIEW_TOP_N` (5) candidates after analysis. Final: 1080×1920, x264 slow CRF 20, ≤1080p
  section; rendered only on approval (or editing an approved clip). Both use loudnorm −14 LUFS,
  AAC 48 kHz, 30 fps and faststart.
- One ffmpeg pass: input-seek cut, layout filtergraph, burned ASS captions, loudness. Captions
  are word-by-word highlighted, the transcript text is sanitised against ASS override injection,
  and lines are placed above the platform-UI safe area.
- The render hash covers renderer version, layout rules, kind, source, times, layout/crop,
  caption words with timings, style, credit and intro. Same hash + existing blob → reuse.
  Renders and their failures go in `clip_renders` (status) and `audit_log` (error). No schema
  change: `clip_renders` has no error column, so errors live in the audit log.
- Render jobs go on their own `render` queue family, so encodes don't block analysis.

## ADR-0034 — Review actions and feedback events (2026-11-21)
`ReviewService` handles approve, reject, reset, trim, caption edit, layout, style, crop and
intro. Every change writes a `feedback_events` row. Approve/reject/trim rows include a snapshot
of what the scorer believed (score, rank, components, judge overall/model): that is the M8
training set. Validation: clip length 5–180 s, inside the video; layouts and crop rects are
checked; approval re-checks takedowns.
`export_description` always appends the source's required credit. A clip with no intro and
unedited captions carries the YouTube reused-content warning (spec §2).

## ADR-0034 addendum — Takedown gate parity (2026-11-21)
Security review: `ensure_clip` and review *edits* weren't gated on takedowns (only approval and
rendering were). Creating or editing a clip after a retire would resurface a clip the takedown
hid. Now `ensure_clip` and every clip-creating or clip-editing review action call
`require_not_retired`. After a takedown only `rejected`/`pending` status changes are allowed,
so the review queue can still be tidied.

## ADR-0035 — Review UI: React + Vite, no framework, static build behind nginx (2026-11-21)
- React 19, Vite and TypeScript (strict), as the spec asks. No UI kit, state library or router:
  five screens don't need them, and every dependency is MIT. Hash routes keep the build static.
- Mobile first, because the owner reviews on a phone. The 9:16 player is height-capped
  (62 dvh), the action bar is fixed with safe-area padding, and touch targets are ≥ 44 px.
  Visual language: a light "edit bay" palette where colour carries meaning. Each signal owns
  a hue, so the signal strip on every moment shows *why* it was picked.
- Media: `<video src>` can't send the bearer header, and putting the token in a URL would
  leak it into logs and history. The UI fetches renders as blobs and plays object URLs. That
  fits previews and finals (a few MB to ~15 MB). Signed short-lived URLs are the SaaS-era
  replacement and only touch the download route.
- The token is stored in localStorage on the device. That's acceptable for a single-owner,
  loopback-bound MVP. Real auth replaces `current_principal` and this screen together.
- Serving: a multi-stage `Dockerfile.web` (node:24-alpine build → nginx:1.27-alpine) with
  `/api` proxied to the API, so there's one origin and no CORS. It sets a strict CSP
  (`media-src blob:`), nosniff and DENY framing. It binds to loopback by default. For phone
  access use a private tunnel, or opt in to LAN with `CLIPFORGE_WEB_BIND`.
- The lockfile must list every platform's optional packages: one generated on Windows failed
  `npm ci` on Linux (missing `@emnapi/*`). Regenerate it from scratch when dependencies change.

## ADR-0036 — Scoring profile travels with the video, by id (2026-11-21)
Bug found while wiring the profile selector: `POST /videos` stored `scoring_profile_id`, but
the worker analysed with the default profile, because the payload carried only an optional
profile *name*. Now the id rides in the job payload (fetch → transcribe → analyze).
`get_scoring_profile` accepts an id (exact version), a name (latest version) or None
(default), so a new version of a profile doesn't change an in-flight analysis.
`POST /videos/{id}/analyze` pins a profile and re-scores; signals are cached, so it's cheap.
The candidate list shows one analysis only (the pinned profile, else the newest candidates'
profile), because mixing profiles would mix score scales. Profiles and caption styles are
append-only and versioned: POSTing an existing name creates version n+1, so old candidates
and renders stay explainable. Validation is pure (`scoring/profile.py`,
`rendering/captions.validate_style_spec`). The caption font is restricted to letters, digits,
spaces and hyphens, even though render-time sanitising already strips ASS separators.

## ADR-0037 — "Latest render" means most recently produced or reused (2026-11-21)
Bug: `latest_render` picked the highest id. With content-hash reuse, trimming A → B → back
to A reuses A's old row, so downloads served B's video. Now every save bumps `updated_at`,
including a cache hit (the render service re-saves the reused row), and `latest_render` orders
by `updated_at`. No schema change: the column already existed. A regression test covers the
A→B→A case.

