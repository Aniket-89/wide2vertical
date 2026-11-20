"""MediaFetcher using yt-dlp. Audio first (spec §4.8): transcription never needs video."""

from __future__ import annotations

import hashlib
import shutil
import sys
from pathlib import Path

import structlog

from clipforge.adapters.proc import ProcError, run, safe_url_arg
from clipforge.adapters.resolvers.urls import EXTRACTOR
from clipforge.domain.values import Platform
from clipforge.ports.pipeline import MIN_COMPLETENESS, FetchedFile, FetchFailed, ResolvedVideo

log = structlog.get_logger(__name__)


def probe_duration(path: Path, timeout_s: float = 60) -> float | None:
    """Container duration via ffprobe, or None if ffprobe is unavailable/unparseable."""
    if shutil.which("ffprobe") is None:
        return None
    args = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "csv=p=0",
        str(path),
    ]
    try:
        return float(run(args, timeout_s=timeout_s).stdout.strip())
    except (ProcError, ValueError):
        return None


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()


class YtDlpFetcher:
    def __init__(
        self,
        timeout_s: float = 3 * 3600,
        retries: int = 3,
        hls_via_ffmpeg: bool | None = None,
    ) -> None:
        self.timeout_s = timeout_s
        self.retries = retries
        # yt-dlp's native HLS downloader fails on Twitch's fMP4 playlists ("Initialization
        # fragment found after media fragments"); ffmpeg handles them (ADR-0016).
        self.hls_via_ffmpeg = (
            shutil.which("ffmpeg") is not None if hls_via_ffmpeg is None else hls_via_ffmpeg
        )

    def build_args(self, url: str, workdir: Path, platform: Platform | None = None) -> list[str]:
        args = [
            sys.executable, "-m", "yt_dlp",
            "-f", "bestaudio/best",
            "--no-playlist", "--no-part", "--no-progress", "--quiet", "--no-warnings",
            "--retries", str(self.retries), "--fragment-retries", str(self.retries),
        ]  # fmt: skip
        if platform is not None:
            args += ["--use-extractors", EXTRACTOR[platform]]
        if self.hls_via_ffmpeg:
            # ffmpeg's HLS keepalive reconnect can silently end the stream after a TLS error;
            # a fresh connection per segment avoids it (ADR-0019).
            args += ["--downloader", "m3u8:ffmpeg",
                     "--downloader-args", "ffmpeg_i:-http_persistent 0"]  # fmt: skip
        try:
            safe = safe_url_arg(url)
        except ValueError as exc:
            raise FetchFailed(str(exc)) from exc
        return [*args, "-o", str(workdir / "audio.%(ext)s"), "--", safe]

    def _download_once(self, video: ResolvedVideo, workdir: Path) -> Path:
        for old in workdir.glob("audio.*"):
            old.unlink()
        try:
            run(self.build_args(video.url, workdir, video.platform), timeout_s=self.timeout_s)
        except ProcError as exc:
            raise FetchFailed(str(exc)) from exc
        files = sorted(p for p in workdir.glob("audio.*") if p.is_file())
        if not files:
            raise FetchFailed("yt-dlp finished but produced no audio file")
        return files[0]

    def fetch_audio(self, video: ResolvedVideo, workdir: Path) -> FetchedFile:
        """Download, then verify length: ffmpeg can end an HLS stream early on a network
        error and still exit 0 (ADR-0018), so a short file is retried, never accepted."""
        expected = video.duration_s
        got: float | None = None
        for attempt in range(1, self.retries + 1):
            f = self._download_once(video, workdir)
            got = probe_duration(f)
            if got is None or not expected or got >= MIN_COMPLETENESS * expected:
                return FetchedFile(
                    path=f,
                    content_hash=sha256_file(f),
                    bytes=f.stat().st_size,
                    ext=f.suffix.lstrip("."),
                    duration_s=got,
                )
            log.warning("fetch_truncated", attempt=attempt, got_s=got, expected_s=expected)
        raise FetchFailed(
            f"download truncated after {self.retries} attempts: {got:.0f}s of {expected:.0f}s"
        )
