"""Re-exports the pure URL rules from the domain, plus yt-dlp-specific extractor names."""

from __future__ import annotations

from clipforge.domain.urls import ParsedUrl, normalise_handle, parse_video_url
from clipforge.domain.values import Platform

# yt-dlp extractor to pin per platform, so it can never fall back to the generic extractor
EXTRACTOR = {Platform.YOUTUBE: "youtube", Platform.TWITCH: "twitch:vod", Platform.KICK: "kick:vod"}

__all__ = ["EXTRACTOR", "ParsedUrl", "normalise_handle", "parse_video_url"]
