"""Shared numpy helpers for signal extractors. Pure functions, 1-second bins."""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np

from clipforge.ports.analysis import FloatArray

EPS = 1e-9


def bin_counts(
    times_s: Iterable[float], n: int, weights: Iterable[float] | None = None
) -> FloatArray:
    """Count (or sum ``weights``) of events per 1 s bin over [0, n)."""
    t = np.fromiter(times_s, dtype=np.float64)
    w = None if weights is None else np.fromiter(weights, dtype=np.float64)
    if t.size == 0 or n <= 0:
        return np.zeros(max(n, 0))
    idx = np.floor(t).astype(np.int64)
    keep = (idx >= 0) & (idx < n)
    out = np.bincount(idx[keep], weights=None if w is None else w[keep], minlength=n)
    return out[:n].astype(np.float64)


def rolling_mean(x: FloatArray, window_s: int) -> FloatArray:
    """Centred moving average; edges use the available samples."""
    if window_s <= 1 or x.size == 0:
        return x.astype(np.float64)
    kernel = np.ones(window_s)
    num = np.convolve(x, kernel, mode="same")
    den = np.convolve(np.ones_like(x), kernel, mode="same")
    return num / np.maximum(den, EPS)


def rolling_median(x: FloatArray, window_s: int) -> FloatArray:
    """Centred rolling median (edge-padded). O(n * window) memory: fine for 1 s bins
    (3 h x 300 s window ~ 26 MB)."""
    if x.size == 0:
        return x.astype(np.float64)
    w = max(1, min(window_s, x.size))
    if w % 2 == 0:
        w += 1 if w < x.size else -1
    w = max(w, 1)
    pad = w // 2
    padded = np.pad(x.astype(np.float64), (pad, pad), mode="edge")
    view = np.lib.stride_tricks.sliding_window_view(padded, w)
    return np.median(view, axis=1)[: x.size]


def robust_spike(x: FloatArray, baseline_s: int, z_half: float) -> FloatArray:
    """How far above its local baseline each second is, in robust z units, mapped to 0..1
    with soft saturation: v = z / (z + z_half), so z = z_half gives 0.5.

    Soft (not clipped) on purpose: a 4-sigma noise blip and a 29-sigma chat explosion must
    not both become 1.0, or peak picking can't tell them apart.

    Baseline = rolling median, scale = rolling median absolute deviation. Using local
    (not global) stats means a stream that gets busier over time doesn't drown early
    moments, and a quiet stream's small spikes still register.
    """
    if x.size == 0:
        return x.astype(np.float64)
    med = rolling_median(x, baseline_s)
    mad = rolling_median(np.abs(x - med), baseline_s)
    scale = 1.4826 * mad
    # floor the scale so a near-constant baseline doesn't turn noise into huge z-scores
    floor = max(
        float(np.median(np.abs(x - np.median(x)))) * 1.4826, 0.05 * float(np.mean(np.abs(x))), EPS
    )
    z = (x - med) / np.maximum(scale, floor)
    zp = np.maximum(z, 0.0)
    return zp / (zp + z_half)


def shift_earlier(x: FloatArray, lag_s: int) -> FloatArray:
    """y[t] = x[t + lag]. Chat reacts several seconds after the moment it reacts to."""
    if lag_s <= 0 or x.size == 0:
        return x
    return np.concatenate([x[lag_s:], np.zeros(min(lag_s, x.size))])


def fit_length(x: FloatArray, n: int, fill: float = 0.0) -> FloatArray:
    if x.size >= n:
        return x[:n].astype(np.float64)
    return np.concatenate([x.astype(np.float64), np.full(n - x.size, fill)])
