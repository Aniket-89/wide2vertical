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
