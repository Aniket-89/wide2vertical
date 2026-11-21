"""Render adapters: ffmpeg filtergraphs/args, HLS playlist logic, and real ffmpeg renders of a
generated test video (skipped where ffmpeg is unavailable)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from clipforge.adapters.media.facecam_opencv import OpenCvFacecamDetector
from clipforge.adapters.media.sections import (
    HlsSegmentFetcher,
    YtDlpSectionFetcher,
    _attrs,
    best_variant,
    parse_media_playlist,
    select_segments,
)
from clipforge.adapters.render.ffmpeg_renderer import (
    FfmpegRenderer,
    build_args,
    build_filtergraph,
)
from clipforge.domain.values import Platform
from clipforge.ports.pipeline import FetchFailed, ResolvedVideo, Word
from clipforge.ports.render import RenderSpec
from clipforge.rendering.captions import CaptionStyle, build_ass, clip_words
from clipforge.rendering.layout import Box, FrameFaces, LayoutPlan, choose_layout

HAS_FFMPEG = shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None
needs_ffmpeg = pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg/ffprobe not installed")


def spec(plan: LayoutPlan, tmp: Path, **kw: object) -> RenderSpec:
    base: dict[str, object] = dict(
        source=tmp / "src.mp4",
        offset_s=2.0,
        duration_s=4.0,
        plan=plan,
        ass="",
        out_w=540,
        out_h=960,
        quality="preview",
        output=tmp / "out.mp4",
    )
    base.update(kw)
    return RenderSpec(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------- filtergraphs / args
def test_filtergraphs_per_layout(tmp_path: Path) -> None:
    full = Box(0, 0, 1920, 1080)
    center = build_filtergraph(
        spec(LayoutPlan("center_crop", None, Box(656, 0, 608, 1080)), tmp_path)
    )
    assert "crop=608:1080:656:0,scale=540:960" in center
    stack = build_filtergraph(
        spec(LayoutPlan("facecam_top_gameplay_bottom", Box(1440, 0, 480, 270), full, 608), tmp_path)
    )
    assert "vstack=inputs=2" in stack and "scale=540:304" in stack and "scale=540:656" in stack
    blur = build_filtergraph(spec(LayoutPlan("gameplay_blur_background", None, full), tmp_path))
    assert "boxblur" in blur and "overlay=(W-w)/2:(H-h)/2" in blur
    for g in (center, stack, blur):
        assert g.endswith("[base]ass=captions.ass,fps=30,format=yuv420p[v]")


def test_args_seek_before_input_and_quality_presets(tmp_path: Path) -> None:
    plan = LayoutPlan("gameplay_blur_background", None, Box(0, 0, 1280, 720))
    a = build_args(spec(plan, tmp_path))
    assert a.index("-ss") < a.index("-i")  # fast input seek
    assert a[a.index("-preset") + 1] == "veryfast" and a[a.index("-crf") + 1] == "28"
    assert "loudnorm=I=-14:TP=-1.5:LRA=11" in a and "0:a?" in a and "+faststart" in a
    f = build_args(spec(plan, tmp_path, quality="final"))
    assert f[f.index("-preset") + 1] == "slow" and f[f.index("-crf") + 1] == "20"


# ---------------------------------------------------------------- HLS logic
PLAYLIST = """#EXTM3U
#EXT-X-VERSION:6
#EXT-X-TARGETDURATION:10
#EXT-X-MAP:URI="v0-init.mp4"
#EXTINF:7.000,
0.mp4
#EXT-X-DISCONTINUITY
#EXT-X-MAP:URI="init-0.mp4"
#EXTINF:10.000,
1.mp4
#EXTINF:10.000,
2.mp4
#EXTINF:10.000,
3.mp4
#EXT-X-ENDLIST
"""


def test_media_playlist_timeline_and_maps() -> None:
    segs = parse_media_playlist(PLAYLIST, "https://cdn.example/vod/720p/index.m3u8")
    assert [s.start_s for s in segs] == [0.0, 7.0, 17.0, 27.0]
    assert segs[0].map_uri == "https://cdn.example/vod/720p/v0-init.mp4"
    assert segs[1].map_uri == "https://cdn.example/vod/720p/init-0.mp4"
    assert segs[2].uri == "https://cdn.example/vod/720p/2.mp4"
    picked = select_segments(segs, 12.0, 20.0)
    assert [s.uri.rsplit("/", 1)[1] for s in picked] == ["1.mp4", "2.mp4"]


def test_master_playlist_variant_choice() -> None:
    master = """#EXTM3U
#EXT-X-STREAM-INF:BANDWIDTH=8000000,RESOLUTION=1920x1080,CODECS="avc1.64002A,mp4a.40.2"
1080p60/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1280x720
720p60/index.m3u8
#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=852x480
480p30/index.m3u8
"""
    base = "https://cdn.example/vod/master.m3u8"
    assert best_variant(master, base, 720) == "https://cdn.example/vod/720p60/index.m3u8"
    assert best_variant(master, base, 2000) == "https://cdn.example/vod/1080p60/index.m3u8"
    assert best_variant(master, base, 100) is None
    assert dict(_attrs('A=1,CODECS="x,y",B=2')) == {"A": "1", "CODECS": "x,y", "B": "2"}


def test_hls_fetcher_refuses_non_https() -> None:
    with pytest.raises(FetchFailed, match="non-https"):
        HlsSegmentFetcher()._get("http://cdn.example/x.mp4", 10)


def test_ytdlp_section_args_are_accurate_and_pinned(tmp_path: Path) -> None:
    rv = ResolvedVideo(
        Platform.YOUTUBE,
        "https://www.youtube.com/watch?v=jNQXAC9IVRw",
        "jNQXAC9IVRw",
        "jawed",
        None,
        "t",
        19.0,
    )
    a = YtDlpSectionFetcher().build_args(rv, 3.0, 15.5, tmp_path, 720)
    assert "--force-keyframes-at-cuts" in a
    assert a[a.index("--download-sections") + 1] == "*3.000-15.500"
    assert a[a.index("--use-extractors") + 1] == "youtube"
    assert a[-2:] == ["--", rv.url]


# ---------------------------------------------------------------- real ffmpeg
@pytest.fixture(scope="module")
def test_video(tmp_path_factory: pytest.TempPathFactory) -> Path:
    if not HAS_FFMPEG:
        pytest.skip("ffmpeg not installed")
    out = tmp_path_factory.mktemp("media") / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
         "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30",
         "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000",
         "-t", "8", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac", "-shortest",
         str(out)], check=True, timeout=120)  # fmt: skip
    return out


def probe(path: Path) -> dict[str, object]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type,width,height:format=duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True, timeout=60).stdout  # fmt: skip
    data = json.loads(out)
    video = next(s for s in data["streams"] if s["codec_type"] == "video")
    return {
        "w": video["width"],
        "h": video["height"],
        "audio": any(s["codec_type"] == "audio" for s in data["streams"]),
        "dur": float(data["format"]["duration"]),
    }


@needs_ffmpeg
@pytest.mark.parametrize(
    "layout", ["center_crop", "gameplay_blur_background", "facecam_top_gameplay_bottom"]
)
def test_real_render_each_layout(test_video: Path, tmp_path: Path, layout: str) -> None:
    faces = FrameFaces(1280, 720, [[Box(1100, 560, 80, 80)]] * 6)  # webcam-ish bottom right
    plan = choose_layout(faces, requested=layout)
    assert plan.layout == layout
    words = [
        Word(w, 0.2 + i * 0.4, 0.5 + i * 0.4, 1)
        for i, w in enumerate(["one", "two", "three", "four"])
    ]
    ass = build_ass(
        clip_words(words, 0, 4),
        CaptionStyle(font="DejaVu Sans"),
        4.0,
        width=540,
        height=960,
        credit="Clip: test",
        intro="intro",
    )
    s = RenderSpec(test_video, 2.0, 4.0, plan, ass, 540, 960, "preview", tmp_path / "o.mp4")
    res = FfmpegRenderer().render(s)
    info = probe(s.output)
    assert (info["w"], info["h"]) == (540, 960)
    assert info["audio"] is True
    assert abs(float(info["dur"]) - 4.0) < 0.3  # type: ignore[arg-type]
    assert res.render_seconds > 0


@needs_ffmpeg
def test_real_final_render_is_1080x1920(test_video: Path, tmp_path: Path) -> None:
    plan = choose_layout(FrameFaces(1280, 720, []), requested="gameplay_blur_background")
    ass = build_ass([], CaptionStyle(font="DejaVu Sans"), 2.0)
    s = RenderSpec(test_video, 0.0, 2.0, plan, ass, 1080, 1920, "final", tmp_path / "f.mp4")
    FfmpegRenderer().render(s)
    info = probe(s.output)
    assert (info["w"], info["h"]) == (1080, 1920)


@needs_ffmpeg
def test_real_facecam_detector_on_faceless_video(test_video: Path) -> None:
    ff = OpenCvFacecamDetector().analyze(test_video, [1.0, 3.0, 5.0])
    assert (ff.width, ff.height) == (1280, 720)
    assert len(ff.samples) == 3
    assert choose_layout(ff).layout == "gameplay_blur_background"
