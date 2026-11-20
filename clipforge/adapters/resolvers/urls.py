"""Pure URL classification. No network. Used by resolvers' ``supports()``."""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urlparse

from clipforge.domain.values import Platform, normalise_handle

_YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")
_TWITCH_VOD = re.compile(r"^/videos/(\d+)/?$")
_KICK_VOD = re.compile(r"^/(?:[\w-]+/videos|video)/([0-9a-fA-F-]{36})/?$")

_YT_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
_TWITCH_HOSTS = {"twitch.tv", "www.twitch.tv", "m.twitch.tv"}
_KICK_HOSTS = {"kick.com", "www.kick.com"}


@dataclass(frozen=True, slots=True)
class ParsedUrl:
    platform: Platform
    external_id: str


def parse_video_url(url: str) -> ParsedUrl | None:
    """Return platform + video id for a supported VOD URL, else None."""
    try:
        u = urlparse(url.strip())
    except ValueError:
        return None
    if u.scheme not in {"http", "https"}:
        return None
    host = (u.hostname or "").lower()

    if host in _YT_HOSTS:
        if u.path == "/watch":
            vid = parse_qs(u.query).get("v", [""])[0]
        else:
            m = re.match(r"^/(?:live|shorts|embed)/([^/?#]+)", u.path)
            vid = m.group(1) if m else ""
        return ParsedUrl(Platform.YOUTUBE, vid) if _YT_ID.match(vid) else None
    if host == "youtu.be":
        vid = u.path.lstrip("/").split("/")[0]
        return ParsedUrl(Platform.YOUTUBE, vid) if _YT_ID.match(vid) else None

    if host in _TWITCH_HOSTS:
        m = _TWITCH_VOD.match(u.path)
        return ParsedUrl(Platform.TWITCH, m.group(1)) if m else None

    if host in _KICK_HOSTS:
        m = _KICK_VOD.match(u.path)
        return ParsedUrl(Platform.KICK, m.group(1).lower()) if m else None

    return None


__all__ = ["ParsedUrl", "normalise_handle", "parse_video_url"]
