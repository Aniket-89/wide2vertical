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
