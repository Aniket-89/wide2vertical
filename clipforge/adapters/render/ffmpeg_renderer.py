"""Renderer: one ffmpeg pass per clip: cut, reframe to 9:16, burn captions, normalise loudness.

Preview: 540x960, x264 veryfast CRF 28. Final: 1080x1920, x264 slow CRF 20. H.264 + AAC,
30 fps, faststart. Captions are written to ``captions.ass`` in the work dir and referenced by
bare name (ffmpeg runs with cwd=workdir), which avoids filter-string escaping of Windows paths.
"""

from __future__ import annotations

import time

from clipforge.adapters.fetchers.ytdlp import probe_duration
from clipforge.adapters.proc import ProcError, run
from clipforge.ports.render import RenderFailed, RenderResult, RenderSpec
from clipforge.rendering.layout import OUT_W, Box

RENDERER_VERSION = "ffmpeg-1"
QUALITY = {
    "preview": {"preset": "veryfast", "crf": "28", "audio_kbps": "128k"},
    "final": {"preset": "slow", "crf": "20", "audio_kbps": "192k"},
}


def _even(v: float) -> int:
    return max(2, round(v / 2) * 2)


def _crop(b: Box) -> str:
    return f"crop={b.w}:{b.h}:{b.x}:{b.y}"


def build_filtergraph(spec: RenderSpec) -> str:
    """Video graph ending in [v]. Pure; unit-tested."""
    p, ow, oh = spec.plan, spec.out_w, spec.out_h
    if p.layout == "center_crop":
        base = f"[0:v]{_crop(p.game)},scale={ow}:{oh}:flags=lanczos,setsar=1[base]"
    elif p.layout == "facecam_top_gameplay_bottom" and p.cam is not None:
        cam_h = _even(p.cam_out_h * ow / OUT_W)
        base = (
            f"[0:v]split=2[c][g];"
            f"[c]{_crop(p.cam)},scale={ow}:{cam_h}:flags=lanczos[top];"
            f"[g]{_crop(p.game)},scale={ow}:{oh - cam_h}:flags=lanczos[bot];"
            f"[top][bot]vstack=inputs=2,setsar=1[base]"
        )
    else:  # gameplay_blur_background
        base = (
            f"[0:v]{_crop(p.game)},split=2[a][b];"
            f"[a]scale={ow}:{oh}:force_original_aspect_ratio=increase,crop={ow}:{oh},"
            f"boxblur=20:2,eq=brightness=-0.08[bg];"
            f"[b]scale={ow}:-2:flags=lanczos[fg];"
            f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1[base]"
        )
    return base + ";[base]ass=captions.ass,fps=30,format=yuv420p[v]"


def build_args(spec: RenderSpec) -> list[str]:
    q = QUALITY[spec.quality]
    return [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{max(0.0, spec.offset_s):.3f}", "-t", f"{spec.duration_s:.3f}",
        "-i", str(spec.source),
        "-filter_complex", build_filtergraph(spec),
        "-map", "[v]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", q["preset"], "-crf", q["crf"],
        "-profile:v", "high", "-pix_fmt", "yuv420p",
        "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
        "-c:a", "aac", "-b:a", q["audio_kbps"], "-ar", "48000",
        "-movflags", "+faststart",
        str(spec.output),
    ]  # fmt: skip


class FfmpegRenderer:
    def __init__(self, timeout_s: float = 1800) -> None:
        self.timeout_s = timeout_s

    @property
    def version(self) -> str:
        return RENDERER_VERSION

    def render(self, spec: RenderSpec) -> RenderResult:
        workdir = spec.output.parent
        (workdir / "captions.ass").write_text(spec.ass, encoding="utf-8")
        t0 = time.monotonic()
        try:
            run(build_args(spec), timeout_s=self.timeout_s, cwd=workdir)
        except ProcError as exc:
            raise RenderFailed(f"ffmpeg render failed: {exc}") from exc
        elapsed = time.monotonic() - t0
        if not spec.output.exists() or spec.output.stat().st_size == 0:
            raise RenderFailed("ffmpeg produced no output")
        got = probe_duration(spec.output) or spec.duration_s
        if got < 0.9 * spec.duration_s:
            raise RenderFailed(f"render short: {got:.1f}s of {spec.duration_s:.1f}s")
        return RenderResult(round(got, 3), round(elapsed, 3))
