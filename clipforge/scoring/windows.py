"""Expand a peak into a clip window, then snap its edges to sentence/pause boundaries."""

from __future__ import annotations

import bisect
import itertools
from collections.abc import Sequence
from dataclasses import dataclass

from clipforge.ports.pipeline import Word

_SENTENCE_END = (".", "!", "?", "…")


@dataclass(frozen=True, slots=True)
class WindowParams:
    min_clip_s: float = 20
    max_clip_s: float = 60
    setup_s: float = 18  # context before the peak (the setup)
    payoff_s: float = 9  # after the peak (the payoff/reaction)
    lead_in_s: float = 1.5
    tail_s: float = 1.0
    snap_tolerance_s: float = 6
    pause_s: float = 0.7  # a gap this long between words counts as a boundary


def speech_boundaries(words: Sequence[Word], pause_s: float) -> list[float]:
    """Times where it's natural to cut: after sentence-final punctuation, or in pauses.
    Returned as the midpoint of the gap between the two words, sorted."""
    out: list[float] = []
    for a, b in itertools.pairwise(words):
        gap = b.start - a.end
        if a.text.rstrip().endswith(_SENTENCE_END) or gap >= pause_s:
            out.append((a.end + max(b.start, a.end)) / 2)
    return out


def _nearest(boundaries: list[float], t: float, lo: float, hi: float) -> float | None:
    i = bisect.bisect_left(boundaries, lo)
    best: float | None = None
    while i < len(boundaries) and boundaries[i] <= hi:
        if best is None or abs(boundaries[i] - t) < abs(best - t):
            best = boundaries[i]
        i += 1
    return best


def make_window(
    peak_s: float, duration_s: float, boundaries: list[float], p: WindowParams
) -> tuple[float, float]:
    """Peak → [start, end]: expand, snap to speech boundaries, pad, enforce length limits."""
    start, end = peak_s - p.setup_s, peak_s + p.payoff_s

    # snap: start must stay before the peak, end after it
    s = _nearest(
        boundaries, start, start - p.snap_tolerance_s, min(start + p.snap_tolerance_s, peak_s - 3)
    )
    e = _nearest(
        boundaries, end, max(end - p.snap_tolerance_s, peak_s + 1), end + p.snap_tolerance_s
    )
    start = (s if s is not None else start) - p.lead_in_s
    end = (e if e is not None else end) + p.tail_s

    # length limits: grow the end first (payoffs run long), then the start
    if end - start < p.min_clip_s:
        need = p.min_clip_s - (end - start)
        grow_end = min(need, duration_s - end)
        end += grow_end
        start -= need - grow_end
    if end - start > p.max_clip_s:  # trim the setup, keep the payoff
        start = end - p.max_clip_s

    start, end = max(0.0, start), min(duration_s, end)
    if end - start < p.min_clip_s:  # pinned at a video edge: extend the other way
        if start <= 0:
            end = min(duration_s, start + p.min_clip_s)
        else:
            start = max(0.0, end - p.min_clip_s)
    return round(start, 2), round(end, 2)
