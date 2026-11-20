"""Pure URL classification + canonicalisation. No network.

Security (ADR-0020): Python's urlparse and yt-dlp can disagree about odd URLs (userinfo,
backslashes, ports), which would let a URL that *looks* like YouTube here reach another
host there. So: reject anything unusual, and hand downstream tools only a canonical URL
rebuilt from the parsed id, never the user's string.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from clipforge.domain.values import Platform, normalise_handle

_YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_TWITCH_VOD = re.compile(r"^/videos/(\d+)/?$")
_KICK_VOD = re.compile(r"^/([A-Za-z0-9_-]{1,64})/videos/([0-9a-fA-F-]{36})/?$")
_UNSAFE = re.compile(r"[\s\\\x00-\x1f\x7f@]")  # whitespace, backslash, control, userinfo

_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
_TWITCH_HOSTS = {"twitch.tv", "www.twitch.tv", "m.twitch.tv"}
_KICK_HOSTS = {"kick.com", "www.kick.com"}

# yt-dlp extractor to pin per platform, so it can never fall back to the generic extractor
EXTRACTOR = {Platform.YOUTUBE: "youtube", Platform.TWITCH: "twitch:vod", Platform.KICK: "kick:vod"}


@dataclass(frozen=True, slots=True)
class ParsedUrl:
    platform: Platform
    external_id: str
    canonical_url: str


def parse_video_url(url: str) -> ParsedUrl | None:
    """Return platform, video id and canonical URL for a supported VOD URL, else None."""
    raw = url.strip()
    if not raw or len(raw) > 2000 or _UNSAFE.search(raw):
        return None
    try:
        u = urlparse(raw)
        port = u.port
    except ValueError:
        return None
    if u.scheme not in {"http", "https"} or port is not None or u.username or u.password:
        return None
    host = (u.hostname or "").lower()

    if host in _YT_HOSTS or host == "youtu.be":
        if host == "youtu.be":
            vid = u.path.lstrip("/").split("/")[0]
        elif u.path == "/watch":
            vid = parse_qs(u.query).get("v", [""])[0]
        else:
            m = re.match(r"^/(?:live|shorts|embed)/([^/?#]+)", u.path)
            vid = m.group(1) if m else ""
        if not _YT_ID.match(vid):
            return None
        return ParsedUrl(Platform.YOUTUBE, vid, f"https://www.youtube.com/watch?v={vid}")

    if host in _TWITCH_HOSTS:
        m = _TWITCH_VOD.match(u.path)
        if not m:
            return None
        return ParsedUrl(Platform.TWITCH, m.group(1), f"https://www.twitch.tv/videos/{m.group(1)}")

    if host in _KICK_HOSTS:
        m = _KICK_VOD.match(u.path)
        if not m:
            return None
        slug, vid = m.group(1).lower(), m.group(2).lower()
        return ParsedUrl(Platform.KICK, vid, f"https://kick.com/{slug}/videos/{vid}")

    return None


__all__ = ["EXTRACTOR", "ParsedUrl", "normalise_handle", "parse_video_url"]
