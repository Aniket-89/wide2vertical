"""Deterministic synthetic stream: steady background with planted 'moments'.

At each moment t: the streamer talks fast and excitedly, audio gets loud, and chat explodes
``chat_lag_s`` seconds later (as real chat does). Used to test signals, scoring and the
replay harness with a known ground truth.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from clipforge.ports.analysis import ChatMessage, FloatArray
from clipforge.ports.pipeline import Word

CALM = ["so", "anyway", "we", "go", "here", "and", "then", "this", "is", "fine", "okay."]
HYPE = ["NO", "WAY!", "what?!", "oh", "my", "god!", "dude!", "INSANE!"]


@dataclass
class Scenario:
    n: int
    moments: list[int]
    words: list[Word]
    chat: list[ChatMessage]
    rms_db: FloatArray


def make_scenario(
    n: int = 1800,
    moments: tuple[int, ...] = (300, 900, 1500),
    chat_lag_s: int = 6,
    seed: int = 7,
) -> Scenario:
    rng = np.random.default_rng(seed)

    # --- speech: ~2.2 words/s calm, ~5 words/s excited for 6 s around each moment
    words: list[Word] = []
    t = 0.5
    i = 0
    while t < n - 1:
        hot = any(m - 3 <= t <= m + 3 for m in moments)
        rate = 5.0 if hot else 2.2
        text = HYPE[i % len(HYPE)] if hot else CALM[i % len(CALM)]
        dur = 0.8 / rate
        words.append(Word(text, round(t, 3), round(t + dur, 3), 0.95))
        gap = 1.2 if (not hot and text.endswith(".")) else 0.05  # sentence pauses
        t += dur + gap + float(rng.uniform(0, 0.15))
        i += 1

    # --- chat: Poisson ~1 msg/s, bursts of ~14 msg/s (mostly emotes) after each moment
    chat: list[ChatMessage] = []
    for sec in range(n):
        burst = any(m + chat_lag_s <= sec < m + chat_lag_s + 8 for m in moments)
        k = int(rng.poisson(14 if burst else 1.0))
        for j in range(k):
            text = (
                rng.choice(["KEKW", "LUL", "OMEGALUL", "W W W", "AAAAAA"])
                if burst
                else rng.choice(["hello", "what game is this", "lol", "nice", "gg"])
            )
            chat.append(ChatMessage(sec + j / max(k, 1), f"u{rng.integers(500)}", str(text)))

    # --- audio: -30 dB ± 1.5 background, +16 dB for 4 s at each moment
    rms = -30.0 + rng.normal(0, 1.5, n)
    for m in moments:
        rms[m : m + 4] += 16.0
    return Scenario(n, list(moments), words, chat, rms)
