"""Validation for user-edited scoring profiles. Pure: dicts in, cleaned dicts out."""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

_KEY_RE = re.compile(r"[a-z][a-z0-9_]{0,39}")
MAX_WEIGHT = 10.0


def _number(key: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, int | float) or not math.isfinite(value):
        raise ValueError(f"{key} must be a finite number")
    return float(value)


def validate_profile(
    weights: Mapping[str, Any], params: Mapping[str, Any]
) -> tuple[dict[str, float], dict[str, Any]]:
    """Weights are relative (re-normalised over available signals), so only their ratios
    matter; at least one must be positive. Params are numbers or booleans with a few range
    checks on the ones that shape clips. Raises ValueError with a user-facing message."""
    if not weights or len(weights) > 20:
        raise ValueError("weights must have 1-20 entries")
    if len(params) > 40:
        raise ValueError("params can have at most 40 entries")
    for key in (*weights, *params):
        if not isinstance(key, str) or not _KEY_RE.fullmatch(key):
            raise ValueError(f"bad key {key!r}: use lower_snake_case")
    w = {k: _number(f"weights.{k}", v) for k, v in weights.items()}
    if any(not 0 <= v <= MAX_WEIGHT for v in w.values()):
        raise ValueError(f"weights must be between 0 and {MAX_WEIGHT:g}")
    if not any(v > 0 for v in w.values()):
        raise ValueError("at least one weight must be positive")
    p: dict[str, Any] = {}
    for k, v in params.items():
        p[k] = v if isinstance(v, bool) else _number(f"params.{k}", v)
    lo, hi = p.get("min_clip_s", 20), p.get("max_clip_s", 60)
    if not 5 <= lo < hi <= 180:
        raise ValueError("need 5 <= min_clip_s < max_clip_s <= 180")
    for k in ("max_clips_per_hour", "judge_top_n_per_hour"):
        if k in p and not 0 <= p[k] <= 200:
            raise ValueError(f"{k} must be between 0 and 200")
    return w, p
