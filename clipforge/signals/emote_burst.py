"""emote_burst: share of emote/spam messages (KEKW, LUL, W, 😂, "AAAAA") in chat, above the
community's usual share, weighted by volume so one lonely "LUL" isn't a burst."""

from __future__ import annotations

import re
from collections.abc import Iterable

import numpy as np

from clipforge.ports.analysis import ChatMessage, SignalContext, SignalSeries
from clipforge.signals._base import EPS, bin_counts, rolling_mean, rolling_median, shift_earlier

DEFAULT_EMOTES = frozenset(
    w.lower()
    for w in (
        "W", "L", "LUL", "LULW", "KEKW", "KEK", "OMEGALUL", "LMAO", "LMFAO", "LOL", "XD",
        "Pog", "PogChamp", "POGGERS", "PogU", "Kappa", "monkaS", "monkaW", "PepeLaugh",
        "Sadge", "ICANT", "Clueless", "Aware", "EZ", "GG", "Clap", "catJAM", "HUH",
        "WTF", "OMG", "NO", "YES", "?", "??", "???", "F",
    )
)  # fmt: skip
_EMOJI = re.compile("[\U0001f300-\U0001faff☀-➿]")
_REPEAT = re.compile(r"(.)\1{3,}")  # AAAA, !!!!, hahahaha-ish


def is_spammy(msg: ChatMessage, emotes: frozenset[str]) -> bool:
    """A message counts as emote/spam if most of its tokens are emotes/emoji/repeats."""
    tokens = msg.text.split()
    if not tokens:
        return bool(msg.emotes)
    provider_emotes = {e.lower() for e in msg.emotes}
    hits = sum(
        1
        for t in tokens
        if t.lower() in emotes
        or t.lower() in provider_emotes
        or _EMOJI.search(t)
        or _REPEAT.search(t)
    )
    return hits / len(tokens) >= 0.5


class EmoteBurst:
    name = "emote_burst"
    version = "1"
    required = False

    def __init__(self, emotes: Iterable[str] = DEFAULT_EMOTES) -> None:
        self.emotes = frozenset(e.lower() for e in emotes)

    def extract(self, ctx: SignalContext) -> SignalSeries | None:
        p = ctx.params
        chat = ctx.chat
        if chat is None or len(chat) < int(p.get("chat_min_messages", 50)):
            return None
        extra = frozenset(str(e).lower() for e in p.get("extra_emotes", ()))
        emotes = self.emotes | extra
        n, w = ctx.n_seconds, int(p.get("chat_smooth_s", 5))
        total = rolling_mean(bin_counts((m.t_s for m in chat), n), w)
        spam = rolling_mean(
            bin_counts(
                (m.t_s for m in chat), n, (1.0 if is_spammy(m, emotes) else 0.0 for m in chat)
            ),
            w,
        )
        share = spam / np.maximum(total, EPS)
        baseline_s = int(p.get("chat_baseline_s", 300))
        usual = rolling_median(share, baseline_s)
        above = np.clip((share - usual) / np.maximum(1.0 - usual, 0.05), 0.0, 1.0)
        typical_rate = max(float(np.median(total[total > 0])) if np.any(total > 0) else 0.0, 0.2)
        volume = np.clip(total / (2.0 * typical_rate), 0.0, 1.0)
        values = shift_earlier(above * volume, int(p.get("chat_lag_s", 6)))
        return SignalSeries(
            self.name,
            self.version,
            values,
            meta={"spam_share_overall": round(float(spam.sum() / max(total.sum(), EPS)), 3)},
        )
