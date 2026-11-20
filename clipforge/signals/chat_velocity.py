"""chat_velocity: messages/second relative to a rolling baseline. The strongest signal on
Twitch/Kick/YouTube live replays: chat explodes a few seconds after something happens."""

from __future__ import annotations

import numpy as np

from clipforge.ports.analysis import SignalContext, SignalSeries
from clipforge.signals._base import (
    bin_counts,
    robust_spike,
    rolling_mean,
    rolling_median,
    shift_earlier,
)


class ChatVelocity:
    name = "chat_velocity"
    version = "1"
    required = False

    def extract(self, ctx: SignalContext) -> SignalSeries | None:
        p = ctx.params
        chat = ctx.chat
        if chat is None or len(chat) < int(p.get("chat_min_messages", 50)):
            return None  # absent or too sparse to mean anything
        counts = bin_counts((m.t_s for m in chat), ctx.n_seconds)
        rate = rolling_mean(counts, int(p.get("chat_smooth_s", 5)))
        spike = robust_spike(
            rate, int(p.get("chat_baseline_s", 300)), float(p.get("chat_z_half", 4.0))
        )
        lag = int(p.get("chat_lag_s", 6))
        values = shift_earlier(spike, lag)

        baseline = rolling_median(rate, int(p.get("chat_baseline_s", 300)))
        ratio = shift_earlier(rate / np.maximum(baseline, 0.05), lag)
        return SignalSeries(
            self.name,
            self.version,
            values,
            meta={
                "messages": len(chat),
                "mean_rate": round(float(counts.mean()), 3),
                "lag_s": lag,
                "ratio": ratio.round(2).tolist(),  # rate / baseline per second, for reasons
            },
        )
