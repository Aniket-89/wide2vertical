"""Peak picking on the combined series."""

from __future__ import annotations

import numpy as np

from clipforge.ports.analysis import FloatArray


def find_peaks(
    x: FloatArray, *, min_distance_s: int, percentile: float, min_height: float
) -> list[int]:
    """Local maxima above max(percentile(x), min_height), highest first, at least
    ``min_distance_s`` apart (greedy: a higher peak suppresses nearby lower ones)."""
    if x.size < 3:
        return []
    threshold = max(float(np.percentile(x, percentile)), min_height)
    left = np.concatenate([[-np.inf], x[:-1]])
    right = np.concatenate([x[1:], [-np.inf]])
    # >= on the left so a flat-topped plateau yields one peak (its first second)
    is_peak = (x > left) & (x >= right) & (x >= threshold)
    candidates = np.flatnonzero(is_peak)
    order = candidates[np.argsort(-x[candidates], kind="stable")]
    picked: list[int] = []
    for i in order:
        if all(abs(int(i) - j) >= min_distance_s for j in picked):
            picked.append(int(i))
    return picked
