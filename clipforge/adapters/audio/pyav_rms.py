"""AudioAnalyzer: per-second RMS loudness via PyAV, streamed (constant memory)."""

from __future__ import annotations

from pathlib import Path

import av
import numpy as np

from clipforge.ports.analysis import FloatArray
from clipforge.ports.pipeline import PipelineError

FLOOR_DB = -100.0


class AudioAnalysisFailed(PipelineError):
    pass


class PyAvRmsAnalyzer:
    def __init__(self, sample_rate: int = 8000) -> None:
        self.sample_rate = sample_rate  # plenty for loudness; keeps decode cheap

    @property
    def version(self) -> str:
        return f"pyav-rms-1:{self.sample_rate}"

    def rms_db_per_second(self, audio: Path) -> FloatArray:
        rate = self.sample_rate
        out: list[float] = []
        carry = np.zeros(0)
        try:
            with av.open(str(audio)) as container:
                stream = container.streams.audio[0]
                resampler = av.AudioResampler(format="s16", layout="mono", rate=rate)
                frames = (f for packet in container.demux(stream) for f in packet.decode())
                for frame in frames:
                    for rf in resampler.resample(frame):  # type: ignore[arg-type]
                        carry = np.concatenate([carry, rf.to_ndarray().reshape(-1) / 32768.0])
                        full = carry.size // rate
                        if full:
                            block = carry[: full * rate].reshape(full, rate)
                            out.extend(self._db(block))
                            carry = carry[full * rate :]
                for rf in resampler.resample(None):
                    carry = np.concatenate([carry, rf.to_ndarray().reshape(-1) / 32768.0])
        except (av.FFmpegError, IndexError, OSError) as exc:
            raise AudioAnalysisFailed(f"audio analysis failed: {exc}") from exc
        if carry.size >= rate // 4:  # keep a meaningful trailing partial second
            out.extend(self._db(carry.reshape(1, -1)))
        return np.asarray(out, dtype=np.float64)

    @staticmethod
    def _db(block: FloatArray) -> list[float]:
        ms = np.mean(np.square(block, dtype=np.float64), axis=1)
        db: FloatArray = np.maximum(10.0 * np.log10(ms + 1e-12), FLOOR_DB)
        return [float(v) for v in db]
