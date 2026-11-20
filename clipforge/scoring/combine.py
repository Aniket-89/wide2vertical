"""Smooth and combine signals with profile weights."""

from __future__ import annotations

from collections.abc import Mapping

import numpy as np

from clipforge.ports.analysis import FloatArray, SignalSeries
from clipforge.signals._base import fit_length, rolling_mean


def combine(
    signals: Mapping[str, SignalSeries],
    weights: Mapping[str, float],
    n: int,
    smoothing_s: int = 3,
) -> tuple[FloatArray, dict[str, float]]:
    """Weighted mean of the *available* signals, re-normalised over their weights.

    Re-normalising matters: if chat is unavailable, a moment with loud audio and fast speech
    should still score high, not be capped at (1 - chat weight).
    Returns (combined 0..1 series, effective weights actually used).
    """
    used = {k: float(w) for k, w in weights.items() if w > 0 and k in signals}
    total = sum(used.values())
    if total <= 0:
        return np.zeros(n), {}
    out = np.zeros(n)
    for name, w in used.items():
        out += (w / total) * rolling_mean(fit_length(signals[name].values, n), smoothing_s)
    return np.clip(out, 0.0, 1.0), {k: round(w / total, 4) for k, w in used.items()}
