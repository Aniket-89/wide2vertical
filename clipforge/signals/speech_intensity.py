"""speech_intensity: talking faster than usual, plus exclamations and hype words, from the
transcript's word timings. The one required signal: it only needs the transcript."""

from __future__ import annotations

import re

import numpy as np

from clipforge.ports.analysis import SignalContext, SignalSeries
from clipforge.signals._base import bin_counts, robust_spike, rolling_mean

HYPE = frozenset(
    [
        "what",
        "no",
        "oh",
        "wow",
        "dude",
        "bro",
        "wait",
        "yo",
        "omg",
        "holy",
        "insane",
        "crazy",
        "clutch",
        "nooo",
        "yes",
        "yesss",
        "lets",
        "go",
        "run",
        "dead",
        "bruh",
        "unbelievable",
    ]
)
_STRIP = re.compile(r"[^\w']+")


def excitement(word: str) -> float:
    """0..1 per word: exclamation/question marks, SHOUTING, hype vocabulary."""
    raw = word.strip()
    bare = _STRIP.sub("", raw).lower()
    score = 0.0
    if "!" in raw:
        score += 0.6
    if "?" in raw:
        score += 0.2
    letters = [c for c in raw if c.isalpha()]
    if len(letters) >= 3 and all(c.isupper() for c in letters):
        score += 0.5
    if bare in HYPE:
        score += 0.4
    return min(score, 1.0)


class SpeechIntensity:
    name = "speech_intensity"
    version = "1"
    required = True

    def extract(self, ctx: SignalContext) -> SignalSeries | None:
        p, n = ctx.params, ctx.n_seconds
        if not ctx.words:
            return SignalSeries(self.name, self.version, np.zeros(n), meta={"words": 0})
        starts = [w.start for w in ctx.words]
        wps = rolling_mean(bin_counts(starts, n), int(p.get("speech_smooth_s", 3)))
        pace = robust_spike(
            wps, int(p.get("speech_baseline_s", 120)), float(p.get("speech_z_half", 2.0))
        )
        hype = rolling_mean(bin_counts(starts, n, (excitement(w.text) for w in ctx.words)), 5)
        hype_n = np.clip(hype / float(p.get("speech_hype_cap", 0.6)), 0.0, 1.0)
        values = np.clip(0.55 * pace + 0.45 * hype_n, 0.0, 1.0)
        return SignalSeries(
            self.name,
            self.version,
            values,
            meta={"words": len(ctx.words), "median_wps": round(float(np.median(wps)), 2)},
        )
