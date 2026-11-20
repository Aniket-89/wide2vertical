"""Transcriber using faster-whisper with word-level timestamps.

Degrades gracefully: if the configured device runs out of memory (or CUDA is missing),
it retries once on CPU/int8 instead of failing the video.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import structlog

from clipforge.ports.pipeline import TranscribeFailed, Transcriber, Transcript, Word

log = structlog.get_logger(__name__)

TRANSCRIBER_VERSION = "fw-1"  # bump when options below change


def _is_resource_error(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return isinstance(exc, MemoryError) or any(
        s in msg for s in ("out of memory", "cuda", "cublas", "cudnn")
    )


class FasterWhisperTranscriber:
    def __init__(
        self,
        model: str = "small",
        device: str = "cpu",
        compute_type: str = "int8",
        vad_filter: bool = True,
    ) -> None:
        self.model_name = model
        self.device = device
        self.compute_type = compute_type
        self.vad_filter = vad_filter
        self._models: dict[tuple[str, str], Any] = {}
        self._lock = threading.Lock()

    @property
    def config_version(self) -> str:
        return f"{TRANSCRIBER_VERSION}:{self.model_name}:vad={int(self.vad_filter)}"

    def _model(self, device: str, compute_type: str) -> Any:
        key = (device, compute_type)
        with self._lock:
            if key not in self._models:
                from faster_whisper import WhisperModel  # heavy import, load lazily

                self._models[key] = WhisperModel(
                    self.model_name, device=device, compute_type=compute_type
                )
            return self._models[key]

    def _run(self, audio: Path, device: str, compute_type: str) -> Transcript:
        model = self._model(device, compute_type)
        segments, info = model.transcribe(
            str(audio), word_timestamps=True, vad_filter=self.vad_filter, beam_size=5
        )
        words: list[Word] = []
        for seg in segments:  # generator: decoding happens here
            for w in seg.words or ():
                words.append(
                    Word(
                        w.word.strip(), round(w.start, 3), round(w.end, 3), round(w.probability, 4)
                    )
                )
        return Transcript(
            language=info.language,
            duration_s=float(info.duration),
            model=self.model_name,
            words=tuple(words),
        )

    def transcribe(self, audio: Path) -> Transcript:
        try:
            return self._run(audio, self.device, self.compute_type)
        except Exception as exc:
            if self.device != "cpu" and _is_resource_error(exc):
                log.warning("whisper_fallback_cpu", error=type(exc).__name__)
                try:
                    return self._run(audio, "cpu", "int8")
                except Exception as exc2:
                    raise TranscribeFailed(f"cpu fallback failed: {exc2}") from exc2
            raise TranscribeFailed(f"{type(exc).__name__}: {exc}") from exc


_: type[Transcriber] = FasterWhisperTranscriber  # static check: satisfies the port
