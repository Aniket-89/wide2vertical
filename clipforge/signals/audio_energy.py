"""audio_energy: loudness spikes (RMS dBFS above a rolling baseline) plus sudden jumps.
Hook for a laughter classifier later: add it as its own signal, don't overload this one."""

from __future__ import annotations

import numpy as np

from clipforge.ports.analysis import SignalContext, SignalSeries
from clipforge.signals._base import fit_length, robust_spike, rolling_mean

SILENCE_DB = -60.0


class AudioEnergy:
    name = "audio_energy"
    version = "1"
    required = False

    def extract(self, ctx: SignalContext) -> SignalSeries | None:
        if ctx.audio_rms_db is None or ctx.audio_rms_db.size == 0:
            return None
        p = ctx.params
        db = np.maximum(fit_length(ctx.audio_rms_db, ctx.n_seconds, SILENCE_DB), SILENCE_DB)
        smooth = rolling_mean(db, int(p.get("audio_smooth_s", 2)))
        spike = robust_spike(
            smooth, int(p.get("audio_baseline_s", 60)), float(p.get("audio_z_half", 3.0))
        )
        jump = np.clip(
            np.diff(smooth, prepend=smooth[:1]) / float(p.get("audio_jump_db", 10.0)), 0, 1
        )
        values = np.clip(0.75 * spike + 0.25 * rolling_mean(jump, 3), 0.0, 1.0)
        return SignalSeries(
            self.name,
            self.version,
            values,
            meta={"median_db": round(float(np.median(db)), 1), "max_db": round(float(db.max()), 1)},
        )
