"""SectionFetchers: get just the part of a VOD a clip needs (spec §4.8: never the whole video).

- YouTube (DASH): ``yt-dlp --download-sections --force-keyframes-at-cuts``, which is accurate.
- Twitch / Kick (HLS): our own segment-level fetch. ffmpeg 5.1/7.1 can't seek in Twitch
  playlists with mid-stream ``EXT-X-MAP`` changes (they stream from the start and output
  nothing, ADR-0031), so we parse the media playlist, pick only the segments overlapping the
  window from their EXTINF timeline, download those with retries, and remux locally. The file's
  VOD start time is known exactly from the timeline.
"""

from __future__ import annotations

import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urljoin, urlparse

import structlog

from clipforge.adapters.fetchers.ytdlp import probe_duration
from clipforge.adapters.proc import ProcError, run, safe_url_arg
from clipforge.adapters.resolvers.urls import EXTRACTOR
from clipforge.domain.values import Platform
from clipforge.ports.pipeline import FetchFailed, ResolvedVideo
from clipforge.ports.render import SectionFile

log = structlog.get_logger(__name__)

MAX_SEGMENT_BYTES = 64 * 1024 * 1024
MAX_PLAYLIST_BYTES = 8 * 1024 * 1024


# ====================================================================== YouTube (yt-dlp)
class YtDlpSectionFetcher:
    def __init__(self, timeout_s: float = 600, retries: int = 2) -> None:
        self.timeout_s = timeout_s
        self.retries = retries

    def build_args(
        self, video: ResolvedVideo, start_s: float, end_s: float, workdir: Path, max_height: int
    ) -> list[str]:
        h = int(max_height)
        return [
            sys.executable, "-m", "yt_dlp", "--quiet", "--no-warnings", "--no-playlist",
            "-f", f"bv*[height<={h}]+ba/b[height<={h}]/b",
            "--download-sections", f"*{start_s:.3f}-{end_s:.3f}",
            "--force-keyframes-at-cuts",  # re-encodes the cut points: frame-accurate start
            "--merge-output-format", "mp4",
            "--use-extractors", EXTRACTOR[video.platform],
            "-o", str(workdir / "section.%(ext)s"),
            "--", safe_url_arg(video.url),
        ]  # fmt: skip

    def fetch_section(
        self, video: ResolvedVideo, start_s: float, end_s: float, workdir: Path, max_height: int
    ) -> SectionFile:
        want = end_s - start_s
        got: float | None = None
        for attempt in range(1, self.retries + 1):
            for old in workdir.glob("section.*"):
                old.unlink()
            try:
                run(
                    self.build_args(video, start_s, end_s, workdir, max_height),
                    timeout_s=self.timeout_s,
                )
            except ProcError as exc:
                raise FetchFailed(f"section download failed: {exc}") from exc
            files = sorted(workdir.glob("section.*"))
            if files:
                got = probe_duration(files[0])
                if got is None or got >= 0.95 * want:
                    return SectionFile(files[0], start_s, got or want)
            log.warning("section_short", attempt=attempt, got_s=got, want_s=want)
        raise FetchFailed(f"section download incomplete: {got}s of {want:.1f}s")


# ====================================================================== HLS (Twitch, Kick)
@dataclass(frozen=True, slots=True)
class Segment:
    start_s: float
    duration_s: float
    uri: str
    map_uri: str | None  # EXT-X-MAP init segment in effect (fMP4), if any


def parse_media_playlist(text: str, base_url: str) -> list[Segment]:
    """EXTINF timeline of an HLS media playlist. Pure; unit-tested."""
    segs: list[Segment] = []
    t = 0.0
    dur: float | None = None
    current_map: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#EXT-X-MAP:"):
            attrs = dict(_attrs(line.split(":", 1)[1]))
            uri = attrs.get("URI")
            current_map = urljoin(base_url, uri) if uri else None
        elif line.startswith("#EXTINF:"):
            dur = float(line.split(":", 1)[1].split(",", 1)[0])
        elif line and not line.startswith("#") and dur is not None:
            segs.append(Segment(round(t, 3), dur, urljoin(base_url, line), current_map))
            t += dur
            dur = None
    return segs


def best_variant(text: str, base_url: str, max_height: int) -> str | None:
    """From a master playlist, the highest variant at or below max_height."""
    best: tuple[int, str] | None = None
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF:") and i + 1 < len(lines):
            attrs = dict(_attrs(line.split(":", 1)[1]))
            res = attrs.get("RESOLUTION", "0x0")
            height = int(res.split("x")[1]) if "x" in res else 0
            if height <= max_height and (best is None or height > best[0]):
                best = (height, urljoin(base_url, lines[i + 1].strip()))
    return best[1] if best else None


def _attrs(s: str) -> Iterator[tuple[str, str]]:
    key, val, in_q, cur = "", "", False, ""
    for ch in s + ",":
        if ch == '"':
            in_q = not in_q
        elif ch == "=" and not in_q and not key:
            key, cur = cur, ""
        elif ch == "," and not in_q:
            val, cur = cur, ""
            if key:
                yield key.strip(), val.strip()
            key = ""
        else:
            cur += ch


def select_segments(segs: list[Segment], start_s: float, end_s: float) -> list[Segment]:
    return [s for s in segs if s.start_s + s.duration_s > start_s and s.start_s < end_s]


class HlsSegmentFetcher:
    def __init__(
        self,
        *,
        timeout_s: float = 30,
        retries: int = 4,
        backoff_s: float = 1.5,
        resolve_timeout_s: float = 90,
    ) -> None:
        self.timeout_s = timeout_s
        self.retries = retries
        self.backoff_s = backoff_s
        self.resolve_timeout_s = resolve_timeout_s

    # -- network
    def _get(self, url: str, limit: int) -> bytes:
        if urlparse(url).scheme != "https":
            raise FetchFailed(f"refusing non-https segment URL: {url[:80]}")
        last: Exception | None = None
        for attempt in range(self.retries):
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})  # noqa: S310
                with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:  # noqa: S310
                    data: bytes = resp.read(limit + 1)
                if len(data) > limit:
                    raise FetchFailed(f"response over {limit} bytes: {url[:80]}")
                return data
            except urllib.error.HTTPError as exc:
                last = exc
                if exc.code in (401, 403, 404, 410):  # expired/forbidden: retrying won't help
                    break
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last = exc  # flaky network / TLS errors: retry with backoff
            time.sleep(self.backoff_s * (2**attempt))
        raise FetchFailed(f"download failed after retries: {type(last).__name__}: {last}")

    def playlist_url(self, video: ResolvedVideo, max_height: int) -> str:
        h = int(max_height)
        args = [sys.executable, "-m", "yt_dlp", "--quiet", "--no-warnings", "-g",
                "-f", f"b[height<={h}]/b", "--use-extractors", EXTRACTOR[video.platform],
                "--", safe_url_arg(video.url)]  # fmt: skip
        try:
            out = run(args, timeout_s=self.resolve_timeout_s).stdout.strip().splitlines()
        except ProcError as exc:
            raise FetchFailed(f"could not resolve stream URL: {exc}") from exc
        if not out or not out[0].startswith("https://"):
            raise FetchFailed("yt-dlp returned no https stream URL")
        return out[0]

    def fetch_section(
        self, video: ResolvedVideo, start_s: float, end_s: float, workdir: Path, max_height: int
    ) -> SectionFile:
        url = self.playlist_url(video, max_height)
        text = self._get(url, MAX_PLAYLIST_BYTES).decode("utf-8", "replace")
        if "#EXT-X-STREAM-INF" in text:  # master playlist: pick a variant
            variant = best_variant(text, url, max_height)
            if variant is None:
                raise FetchFailed("no HLS variant at or below the requested height")
            url = variant
            text = self._get(url, MAX_PLAYLIST_BYTES).decode("utf-8", "replace")
        segs = select_segments(parse_media_playlist(text, url), start_s, end_s)
        if not segs:
            raise FetchFailed(f"no HLS segments cover {start_s:.0f}-{end_s:.0f}s")

        # Group consecutive segments sharing an init map; each group is a valid file on its own.
        groups: list[list[Segment]] = []
        for s in segs:
            if groups and groups[-1][0].map_uri == s.map_uri:
                groups[-1].append(s)
            else:
                groups.append([s])
        parts: list[Path] = []
        for gi, group in enumerate(groups):
            ext = ".mp4" if group[0].map_uri else ".ts"
            part = workdir / f"hls_part{gi:02d}{ext}"
            with part.open("wb") as f:
                if group[0].map_uri:
                    f.write(self._get(group[0].map_uri, MAX_SEGMENT_BYTES))
                for s in group:
                    f.write(self._get(s.uri, MAX_SEGMENT_BYTES))
            parts.append(part)

        out = workdir / "section.mp4"
        try:
            if len(parts) == 1:
                run(
                    [
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-i",
                        str(parts[0]),
                        "-c",
                        "copy",
                        "-movflags",
                        "+faststart",
                        str(out),
                    ],
                    timeout_s=300,
                )
            else:
                listing = workdir / "parts.txt"
                listing.write_text("".join(f"file '{p.name}'\n" for p in parts))
                run(
                    [
                        "ffmpeg",
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-y",
                        "-f",
                        "concat",
                        "-safe",
                        "0",
                        "-i",
                        "parts.txt",
                        "-c",
                        "copy",
                        "-movflags",
                        "+faststart",
                        str(out),
                    ],
                    timeout_s=300,
                    cwd=workdir,
                )
        except ProcError as exc:
            raise FetchFailed(f"remuxing HLS segments failed: {exc}") from exc
        expected = sum(s.duration_s for s in segs)
        got = probe_duration(out)
        if got is not None and got < 0.9 * expected:
            raise FetchFailed(f"HLS section short: {got:.1f}s of {expected:.1f}s")
        return SectionFile(out, segs[0].start_s, got or expected)


# ====================================================================== routing
class RoutingSectionFetcher:
    """YouTube → yt-dlp sections; HLS platforms → segment fetcher."""

    def __init__(
        self, youtube: YtDlpSectionFetcher | None = None, hls: HlsSegmentFetcher | None = None
    ) -> None:
        self.youtube = youtube or YtDlpSectionFetcher()
        self.hls = hls or HlsSegmentFetcher()

    def fetch_section(
        self, video: ResolvedVideo, start_s: float, end_s: float, workdir: Path, max_height: int
    ) -> SectionFile:
        if video.platform is Platform.YOUTUBE:
            return self.youtube.fetch_section(video, start_s, end_s, workdir, max_height)
        return self.hls.fetch_section(video, start_s, end_s, workdir, max_height)
