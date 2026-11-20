"""Subprocess safety and the faster-whisper OOM→CPU fallback (no model download)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

from clipforge.adapters.proc import ProcError, run
from clipforge.adapters.transcribers.faster_whisper import FasterWhisperTranscriber
from clipforge.ports.pipeline import TranscribeFailed, Transcript


def test_run_rejects_shell_strings() -> None:
    with pytest.raises(TypeError):
        run("echo hi", timeout_s=5)  # type: ignore[arg-type]


def test_run_timeout() -> None:
    with pytest.raises(ProcError, match="timed out"):
        run([sys.executable, "-c", "import time; time.sleep(5)"], timeout_s=0.5)


def test_run_nonzero_exit_has_bounded_stderr() -> None:
    code = "import sys; sys.stderr.write('x' * 100000); sys.exit(3)"
    with pytest.raises(ProcError) as ei:
        run([sys.executable, "-c", code], timeout_s=10)
    assert ei.value.returncode == 3
    assert len(str(ei.value)) < 5000


def test_whisper_falls_back_to_cpu_on_cuda_oom(monkeypatch: pytest.MonkeyPatch) -> None:
    t = FasterWhisperTranscriber(model="tiny", device="cuda", compute_type="float16")
    calls: list[tuple[str, str]] = []

    def fake_run(self: Any, audio: Path, device: str, compute_type: str) -> Transcript:
        calls.append((device, compute_type))
        if device == "cuda":
            raise RuntimeError("CUDA failed with error out of memory")
        return Transcript("en", 1.0, "tiny", ())

    monkeypatch.setattr(FasterWhisperTranscriber, "_run", fake_run)
    out = t.transcribe(Path("a.wav"))
    assert out.model == "tiny"
    assert calls == [("cuda", "float16"), ("cpu", "int8")]


def test_whisper_non_resource_error_is_not_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    t = FasterWhisperTranscriber(model="tiny", device="cuda")
    calls: list[str] = []

    def fake_run(self: Any, audio: Path, device: str, compute_type: str) -> Transcript:
        calls.append(device)
        raise ValueError("corrupt audio")

    monkeypatch.setattr(FasterWhisperTranscriber, "_run", fake_run)
    with pytest.raises(TranscribeFailed, match="corrupt audio"):
        t.transcribe(Path("a.wav"))
    assert calls == ["cuda"]


def test_whisper_config_version_tracks_model() -> None:
    a = FasterWhisperTranscriber(model="small").config_version
    b = FasterWhisperTranscriber(model="medium").config_version
    assert a != b


def test_fetcher_routes_hls_through_ffmpeg_when_available(tmp_path: Path) -> None:
    from clipforge.adapters.fetchers.ytdlp import YtDlpFetcher

    with_ff = YtDlpFetcher(hls_via_ffmpeg=True).build_args("https://x", tmp_path)
    without = YtDlpFetcher(hls_via_ffmpeg=False).build_args("https://x", tmp_path)
    assert with_ff[with_ff.index("--downloader") + 1] == "m3u8:ffmpeg"
    assert "ffmpeg_i:-http_persistent 0" in with_ff
    assert "--downloader" not in without
    assert with_ff[-1] == "https://x"


def test_fetcher_without_ffmpeg_fails_fast_on_truncation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from clipforge.adapters.fetchers import ytdlp as mod
    from clipforge.domain.values import Platform
    from clipforge.ports.pipeline import FetchFailed, ResolvedVideo

    rv = ResolvedVideo(Platform.YOUTUBE, "https://youtu.be/jNQXAC9IVRw", "x", "s", None, "t", 100.0)

    def fake_download(self: Any, video: Any, workdir: Path, **kw: Any) -> Path:
        f = workdir / "audio.m4a"
        f.write_bytes(b"x")
        return f

    monkeypatch.setattr(mod.YtDlpFetcher, "_download_once", fake_download)
    monkeypatch.setattr(mod, "probe_duration", lambda p: 50.0)
    with pytest.raises(FetchFailed, match="truncated after 3 attempts"):
        mod.YtDlpFetcher(hls_via_ffmpeg=False).fetch_audio(rv, tmp_path)


def test_fetcher_retries_truncated_download(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from clipforge.adapters.fetchers import ytdlp as mod
    from clipforge.domain.values import Platform
    from clipforge.ports.pipeline import FetchFailed, ResolvedVideo

    rv = ResolvedVideo(
        Platform.TWITCH, "https://www.twitch.tv/videos/1", "1", "s", None, "t", 3024.0
    )
    calls: list[int] = []

    def fake_download(self: Any, video: Any, workdir: Path) -> Path:
        calls.append(1)
        f = workdir / "audio.mp4"
        f.write_bytes(b"x")
        return f

    monkeypatch.setattr(mod.YtDlpFetcher, "_download_once", fake_download)
    durations = iter([684.8, 3024.0])  # first attempt truncated, second complete
    monkeypatch.setattr(mod, "probe_duration", lambda p: next(durations))
    out = mod.YtDlpFetcher(hls_via_ffmpeg=True, retries=3).fetch_audio(rv, tmp_path)
    assert out.duration_s == 3024.0
    assert len(calls) == 2

    monkeypatch.setattr(mod, "probe_duration", lambda p: 600.0)
    with pytest.raises(FetchFailed, match="truncated after 3 attempts"):
        mod.YtDlpFetcher(hls_via_ffmpeg=True, retries=3).fetch_audio(rv, tmp_path)


@pytest.mark.parametrize("bad", ["--exec=rm -rf /", "-o/etc/x", "file:///etc/passwd", "ftp://x"])
def test_url_arguments_cannot_smuggle_options(bad: str, tmp_path: Path) -> None:
    from clipforge.adapters.fetchers.ytdlp import YtDlpFetcher
    from clipforge.adapters.proc import safe_url_arg
    from clipforge.ports.pipeline import FetchFailed

    with pytest.raises(ValueError):
        safe_url_arg(bad)
    with pytest.raises(FetchFailed):
        YtDlpFetcher(hls_via_ffmpeg=False).build_args(bad, tmp_path)


def test_url_follows_end_of_options_marker(tmp_path: Path) -> None:
    from clipforge.adapters.fetchers.ytdlp import YtDlpFetcher

    args = YtDlpFetcher(hls_via_ffmpeg=False).build_args("https://x.test/v", tmp_path)
    assert args[-2:] == ["--", "https://x.test/v"]


def test_untrusted_webpage_url_is_ignored() -> None:
    from clipforge.adapters.resolvers.ytdlp import info_to_resolved
    from clipforge.domain.values import Platform

    info = {"id": "v1", "uploader_id": "s", "webpage_url": "--exec=evil"}
    rv = info_to_resolved(Platform.TWITCH, "https://www.twitch.tv/videos/1", info)
    assert rv.url == "https://www.twitch.tv/videos/1"
