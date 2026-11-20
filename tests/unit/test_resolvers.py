import json
from pathlib import Path

import pytest

from clipforge.adapters.resolvers.urls import parse_video_url
from clipforge.adapters.resolvers.ytdlp import (
    KickResolver,
    TwitchResolver,
    YouTubeResolver,
    info_to_resolved,
)
from clipforge.domain.values import Platform
from clipforge.ports.pipeline import ResolveFailed

FIXTURES = Path(__file__).parent.parent / "fixtures" / "ytdlp"
KICK_ID = "0b7c7c3e-1f2a-4b5c-9d8e-112233445566"


@pytest.mark.parametrize(
    ("url", "platform", "ext_id"),
    [
        ("https://www.youtube.com/watch?v=jNQXAC9IVRw", Platform.YOUTUBE, "jNQXAC9IVRw"),
        ("https://youtube.com/watch?v=jNQXAC9IVRw&t=10s", Platform.YOUTUBE, "jNQXAC9IVRw"),
        ("https://youtu.be/jNQXAC9IVRw", Platform.YOUTUBE, "jNQXAC9IVRw"),
        ("https://www.youtube.com/live/vAbC12345_-", Platform.YOUTUBE, "vAbC12345_-"),
        ("https://m.youtube.com/shorts/jNQXAC9IVRw", Platform.YOUTUBE, "jNQXAC9IVRw"),
        ("https://www.twitch.tv/videos/2245123456", Platform.TWITCH, "2245123456"),
        ("https://m.twitch.tv/videos/2245123456/", Platform.TWITCH, "2245123456"),
        (f"https://kick.com/somebody/videos/{KICK_ID}", Platform.KICK, KICK_ID),
        (f"https://www.kick.com/SomeBody/videos/{KICK_ID.upper()}", Platform.KICK, KICK_ID),
    ],
)
def test_parse_supported_urls(url: str, platform: Platform, ext_id: str) -> None:
    p = parse_video_url(url)
    assert p is not None
    assert (p.platform, p.external_id) == (platform, ext_id)


@pytest.mark.parametrize(
    "url",
    [
        "https://www.youtube.com/@channel",
        "https://www.youtube.com/watch?v=short",
        "https://www.twitch.tv/somestreamer",  # live channel page, not a VOD
        "https://www.twitch.tv/somestreamer/clip/Abc",
        "https://kick.com/somebody",
        "ftp://youtube.com/watch?v=jNQXAC9IVRw",
        "https://evil.example/watch?v=jNQXAC9IVRw",
        "not a url",
        # parser-differential / SSRF shapes (ADR-0020)
        "https://evil.example\\@www.youtube.com/watch?v=jNQXAC9IVRw",
        "https://user@www.youtube.com/watch?v=jNQXAC9IVRw",
        "https://user:pw@www.twitch.tv/videos/1",
        "https://www.youtube.com:8443/watch?v=jNQXAC9IVRw",
        "https://www.twitch.tv/videos/1 --exec=x",
        "https://www.twitch.tv/vid\neos/1",  # embedded control char (edges are stripped)
        "https://www.twitch.tv\\videos\\1",
        f"https://kick.com/video/{KICK_ID}",  # yt-dlp's kick:vod needs the channel slug
    ],
)
def test_parse_rejects(url: str) -> None:
    assert parse_video_url(url) is None


@pytest.mark.parametrize(
    ("url", "canonical"),
    [
        ("https://youtu.be/jNQXAC9IVRw?t=3", "https://www.youtube.com/watch?v=jNQXAC9IVRw"),
        ("https://m.youtube.com/shorts/jNQXAC9IVRw", "https://www.youtube.com/watch?v=jNQXAC9IVRw"),
        ("https://m.twitch.tv/videos/42/?x=1", "https://www.twitch.tv/videos/42"),
        (
            f"https://www.kick.com/SomeBody/videos/{KICK_ID}",
            f"https://kick.com/somebody/videos/{KICK_ID}",
        ),
    ],
)
def test_canonical_url_is_rebuilt_from_parts(url: str, canonical: str) -> None:
    p = parse_video_url(url)
    assert p is not None and p.canonical_url == canonical


def test_resolvers_only_claim_their_platform() -> None:
    yt, tw, kk = YouTubeResolver(), TwitchResolver(), KickResolver()
    url = "https://www.twitch.tv/videos/1"
    assert (yt.supports(url), tw.supports(url), kk.supports(url)) == (False, True, False)


def _load(name: str) -> dict[str, object]:
    return json.loads((FIXTURES / name).read_text())


def test_youtube_info_mapping() -> None:
    rv = info_to_resolved(
        Platform.YOUTUBE, "https://www.youtube.com/watch?v=jNQXAC9IVRw", _load("youtube.json")
    )
    assert rv.external_id == "jNQXAC9IVRw"
    assert rv.channel_handle == "jawed"
    assert rv.channel_id == "UC4QobU6STFB0P71PMvOGN5A"
    assert rv.duration_s == 19
    assert not rv.is_live


def test_youtube_id_starting_with_v_is_preserved() -> None:
    info = {**_load("youtube.json"), "id": "vAbC12345_-"}
    url = "https://www.youtube.com/watch?v=vAbC12345_-"
    assert info_to_resolved(Platform.YOUTUBE, url, info).external_id == "vAbC12345_-"


def test_twitch_info_mapping_strips_v_prefix() -> None:
    rv = info_to_resolved(
        Platform.TWITCH, "https://www.twitch.tv/videos/2929921638", _load("twitch.json")
    )  # recorded sample
    assert rv.external_id == "2929921638"
    assert rv.channel_handle == "twitch"
    assert rv.duration_s == 3024


def test_kick_info_mapping() -> None:
    rv = info_to_resolved(
        Platform.KICK,
        "https://kick.com/somebody/videos/0b7c7c3e-1f2a-4b5c-9d8e-112233445566",
        _load("kick.json"),
    )
    assert rv.channel_handle == "somebody"


def test_missing_channel_fails_loudly() -> None:
    with pytest.raises(ResolveFailed):
        info_to_resolved(
            Platform.TWITCH, "https://www.twitch.tv/videos/2929921638", {"id": "v1", "title": "x"}
        )


def test_resolved_url_is_canonical_not_remote_webpage_url() -> None:
    info = {**_load("twitch.json"), "webpage_url": "https://evil.example/x"}
    rv = info_to_resolved(Platform.TWITCH, "https://m.twitch.tv/videos/2929921638/", info)
    assert rv.url == "https://www.twitch.tv/videos/2929921638"


def test_fetch_and_resolve_pin_the_extractor(tmp_path: Path) -> None:
    from clipforge.adapters.fetchers.ytdlp import YtDlpFetcher

    args = YtDlpFetcher(hls_via_ffmpeg=False).build_args(
        "https://www.twitch.tv/videos/2929921638", tmp_path, Platform.TWITCH
    )
    assert args[args.index("--use-extractors") + 1] == "twitch:vod"
