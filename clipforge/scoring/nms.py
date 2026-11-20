"""Non-max suppression for clip windows."""

from __future__ import annotations

from collections.abc import Sequence

from clipforge.ports.analysis import CandidateWindow


def iou(a: CandidateWindow, b: CandidateWindow) -> float:
    inter = max(0.0, min(a.end_s, b.end_s) - max(a.start_s, b.start_s))
    union = (a.end_s - a.start_s) + (b.end_s - b.start_s) - inter
    return inter / union if union > 0 else 0.0


def nms(
    windows: Sequence[CandidateWindow],
    *,
    iou_threshold: float,
    min_gap_s: float,
    max_count: int,
) -> list[CandidateWindow]:
    """Keep the best-scoring windows; drop any that overlap a kept one too much or whose
    peak is within ``min_gap_s`` of a kept peak. Stops at ``max_count``."""
    kept: list[CandidateWindow] = []
    for w in sorted(windows, key=lambda c: c.score, reverse=True):
        if len(kept) >= max_count:
            break
        if all(iou(w, k) < iou_threshold and abs(w.peak_s - k.peak_s) >= min_gap_s for k in kept):
            kept.append(w)
    return kept
