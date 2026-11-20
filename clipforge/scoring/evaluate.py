"""Replay harness metrics: precision@K / recall@K against hand-labelled good moments."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class EvalResult:
    k: int
    precision: float
    recall: float
    hits: int  # top-K candidates that contain a labelled moment
    covered: int  # labelled moments found by the top-K
    labels: int


def evaluate_at_k(
    ranked: Sequence[tuple[float, float]],
    labels_s: Sequence[float],
    k: int,
    tolerance_s: float = 5.0,
) -> EvalResult:
    """A candidate [start, end] is a hit if any label lies within it (± tolerance).

    precision@K = hits / min(K, #candidates)   (0 if there are no candidates)
    recall@K    = labels covered by top-K / #labels
    """
    top = list(ranked[:k])

    def contains(w: tuple[float, float], t: float) -> bool:
        return w[0] - tolerance_s <= t <= w[1] + tolerance_s

    hits = sum(1 for w in top if any(contains(w, t) for t in labels_s))
    covered = sum(1 for t in labels_s if any(contains(w, t) for w in top))
    precision = hits / len(top) if top else 0.0
    recall = covered / len(labels_s) if labels_s else 0.0
    return EvalResult(k, round(precision, 4), round(recall, 4), hits, covered, len(labels_s))
