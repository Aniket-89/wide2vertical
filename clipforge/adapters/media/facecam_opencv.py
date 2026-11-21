"""FacecamDetector: OpenCV Haar frontal-face detection on frames decoded with PyAV.

Haar is old but ships with OpenCV 4.x (no model download, ADR-0031) and is good enough to find
a streamer's webcam overlay, which is frontal, well lit and in a fixed position. Detection runs
on frames downscaled to ``analysis_width`` and boxes are scaled back to source pixels.
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import av
import cv2
import numpy as np
import numpy.typing as npt

from clipforge.rendering.layout import Box, FrameFaces


class OpenCvFacecamDetector:
    def __init__(self, analysis_width: int = 960, min_face_px: int = 28) -> None:
        data_dir = Path(cv2.data.haarcascades)  # type: ignore[attr-defined]
        cascade = str(data_dir / "haarcascade_frontalface_default.xml")
        self._clf = cv2.CascadeClassifier(cascade)
        if self._clf.empty():
            raise RuntimeError("OpenCV face cascade missing (needs opencv 4.x)")
        self.analysis_width = analysis_width
        self.min_face_px = min_face_px

    def _faces(self, frame: npt.NDArray[np.uint8], scale: float) -> list[Box]:
        gray = cv2.cvtColor(frame, cv2.COLOR_RGB2GRAY)
        gray = cv2.equalizeHist(gray)
        found = self._clf.detectMultiScale(
            gray, scaleFactor=1.1, minNeighbors=6, minSize=(self.min_face_px, self.min_face_px)
        )
        return [
            Box(int(x / scale), int(y / scale), int(w / scale), int(h / scale))
            for (x, y, w, h) in found
        ]

    def analyze(self, video: Path, times_s: Sequence[float]) -> FrameFaces:
        wanted = sorted(t for t in times_s if t >= 0)
        samples: list[list[Box]] = []
        width = height = 0
        with av.open(str(video)) as container:
            stream = container.streams.video[0]
            width, height = stream.codec_context.width, stream.codec_context.height
            scale = min(1.0, self.analysis_width / max(width, 1))
            for t in wanted:
                ts = int(t / float(stream.time_base)) if stream.time_base else 0
                container.seek(ts, stream=stream, any_frame=False, backward=True)
                for frame in container.decode(stream):
                    if frame.time is not None and frame.time + 0.05 < t:
                        continue  # decode forward from the keyframe to the wanted time
                    img = frame.to_ndarray(
                        format="rgb24",
                        width=max(2, int(width * scale)),
                        height=max(2, int(height * scale)),
                    )
                    samples.append(self._faces(np.asarray(img, dtype=np.uint8), scale))
                    break
        return FrameFaces(width, height, samples)
