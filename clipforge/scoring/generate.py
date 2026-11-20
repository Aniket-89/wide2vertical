"""Signals + transcript + profile → ranked candidate windows."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

import numpy as np

from clipforge.ports.analysis import CandidateWindow, FloatArray, SignalSeries
from clipforge.ports.pipeline import Word
from clipforge.scoring.combine import combine
from clipforge.scoring.nms import nms
from clipforge.scoring.peaks import find_peaks
from clipforge.scoring.windows import WindowParams, make_window, speech_boundaries
from clipforge.signals._base import fit_length, rolling_mean

_PHRASES = {
    "chat_velocity": "chat spiked",
    "emote_burst": "emote spam",
    "audio_energy": "loud audio",
    "speech_intensity": "excited speech",
    "llm_moment_score": "LLM flagged it",
    "keyword_boost": "keyword hit",
}


def window_params(params: Mapping[str, Any]) -> WindowParams:
    fields = WindowParams.__dataclass_fields__
    return WindowParams(**{k: float(v) for k, v in params.items() if k in fields})


def _reason(components: Mapping[str, float], signals: Mapping[str, SignalSeries], peak: int) -> str:
    top = [k for k, v in sorted(components.items(), key=lambda kv: -kv[1]) if v >= 0.25][:3]
    if not top:
        return "mild combined activity"
    parts = []
    for k in top:
        text = _PHRASES.get(k, k)
        ratio = signals[k].meta.get("ratio") if k == "chat_velocity" else None
        if isinstance(ratio, list) and 0 <= peak < len(ratio):
            text += f" ({ratio[peak]:.1f}x baseline)"
        parts.append(text)
    return "; ".join(parts)


def generate_candidates(
    signals: Mapping[str, SignalSeries],
    words: Sequence[Word],
    duration_s: float,
    weights: Mapping[str, float],
    params: Mapping[str, Any],
) -> tuple[list[CandidateWindow], FloatArray, dict[str, float]]:
    """Returns (ranked candidates, combined series, effective weights)."""
    n = max(1, math.ceil(duration_s))
    smoothing = int(params.get("smoothing_s", 3))
    combined, used = combine(signals, weights, n, smoothing)
    wp = window_params(params)
    min_gap = float(params.get("min_gap_s", 30))
    peaks = find_peaks(
        combined,
        min_distance_s=max(1, int(min_gap // 2)),
        percentile=float(params.get("peak_percentile", 90)),
        min_height=float(params.get("min_peak", 0.12)),
    )
    boundaries = speech_boundaries(words, wp.pause_s)
    smoothed = {k: rolling_mean(fit_length(signals[k].values, n), smoothing) for k in used}

    windows: list[CandidateWindow] = []
    for pk in peaks:
        start, end = make_window(float(pk), float(n), boundaries, wp)
        a, b = int(start), max(int(start) + 1, math.ceil(end))
        comps = {k: round(float(v[a:b].max()), 4) for k, v in smoothed.items()}
        score = 0.6 * float(combined[pk]) + 0.4 * float(combined[a:b].mean())
        windows.append(
            CandidateWindow(
                start, end, float(pk), round(score, 4), comps, _reason(comps, signals, pk)
            )
        )

    hours = max(duration_s / 3600.0, 1 / 60)
    max_count = max(1, math.ceil(hours * float(params.get("max_clips_per_hour", 12))))
    ranked = nms(
        windows,
        iou_threshold=float(params.get("nms_iou", 0.3)),
        min_gap_s=min_gap,
        max_count=max_count,
    )
    return ranked, combined, used


def summarize(combined: FloatArray) -> dict[str, float]:
    return {
        "p50": round(float(np.percentile(combined, 50)), 3),
        "p90": round(float(np.percentile(combined, 90)), 3),
        "max": round(float(combined.max()), 3),
    }
