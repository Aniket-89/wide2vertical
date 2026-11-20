"""SourceResolvers backed by ``yt-dlp -J`` (metadata only, no download).

One implementation, three platform-specific subclasses (the spec's YouTube/Twitch/Kick
resolvers). They differ in which yt-dlp field holds the channel handle.
"""

from __future__ import annotations

import json
import sys
from typing import Any, ClassVar

from clipforge.adapters.proc import ProcError, run, safe_url_arg
from clipforge.adapters.resolvers.urls import normalise_handle, parse_video_url
from clipforge.domain.values import Platform
from clipforge.ports.pipeline import ResolvedVideo, ResolveFailed, UnsupportedUrl


def _trusted_url(candidate: object, platform: Platform) -> str | None:
    """yt-dlp's webpage_url is remote data; keep it only if it parses as a supported VOD
    URL for the same platform."""
    if not isinstance(candidate, str):
        return None
    parsed = parse_video_url(candidate)
    return candidate if parsed is not None and parsed.platform is platform else None


def info_to_resolved(platform: Platform, url: str, info: dict[str, Any]) -> ResolvedVideo:
    """Map yt-dlp's info dict to a ResolvedVideo. Pure: unit-tested with recorded samples."""
    if platform is Platform.YOUTUBE:
        # uploader_id is "@handle" for modern channels; fall back to channel id.
        handle = info.get("uploader_id") or info.get("channel_id") or ""
    elif platform is Platform.TWITCH:
        handle = info.get("uploader_id") or info.get("uploader") or ""
    else:  # Kick (yt-dlp kick extractor; fragile, see ADR-0006)
        handle = info.get("channel") or info.get("uploader_id") or info.get("uploader") or ""
    if not handle:
        raise ResolveFailed(f"could not determine channel for {url}")
    ext_id = str(info.get("id") or "")
    if not ext_id:
        raise ResolveFailed(f"yt-dlp returned no id for {url}")
    if platform is Platform.TWITCH:
        ext_id = ext_id.removeprefix("v")  # twitch VOD ids come back as "v123"
    duration = info.get("duration")
    return ResolvedVideo(
        platform=platform,
        url=_trusted_url(info.get("webpage_url"), platform) or url,
        external_id=ext_id,
        channel_handle=normalise_handle(str(handle)),
        channel_id=info.get("channel_id"),
        title=str(info.get("title") or ""),
        duration_s=float(duration) if duration is not None else None,
        is_live=bool(info.get("is_live")),
    )


class YtDlpResolver:
    PLATFORM: ClassVar[Platform]

    def __init__(self, timeout_s: float = 60) -> None:
        self.platform: Platform = type(self).PLATFORM
        self.timeout_s = timeout_s

    def supports(self, url: str) -> bool:
        p = parse_video_url(url)
        return p is not None and p.platform is self.platform

    def resolve(self, url: str) -> ResolvedVideo:
        if not self.supports(url):
            raise UnsupportedUrl(url)
        args = [
            sys.executable,
            "-m",
            "yt_dlp",
            "-J",
            "--no-playlist",
            "--skip-download",
            "--no-warnings",
            "--",
            safe_url_arg(url),
        ]
        try:
            out = run(args, timeout_s=self.timeout_s).stdout
            info = json.loads(out)
        except (ProcError, json.JSONDecodeError) as exc:
            raise ResolveFailed(str(exc)) from exc
        return info_to_resolved(self.platform, url, info)


class YouTubeResolver(YtDlpResolver):
    PLATFORM = Platform.YOUTUBE


class TwitchResolver(YtDlpResolver):
    PLATFORM = Platform.TWITCH


class KickResolver(YtDlpResolver):
    PLATFORM = Platform.KICK
