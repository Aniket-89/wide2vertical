"""Each signal on the synthetic scenario: peaks where the planted moments are."""

from __future__ import annotations

import numpy as np
import pytest

from clipforge.ports.analysis import ChatMessage, SignalContext
from clipforge.signals import AudioEnergy, ChatVelocity, EmoteBurst, SpeechIntensity
from clipforge.signals._base import (
    bin_counts,
    robust_spike,
    rolling_mean,
    rolling_median,
    shift_earlier,
)
from clipforge.signals.emote_burst import DEFAULT_EMOTES, is_spammy
from clipforge.signals.speech_intensity import excitement
from tests.scenario import make_scenario

SC = make_scenario()


def ctx(**kw: object) -> SignalContext:
    base: dict[str, object] = dict(
        n_seconds=SC.n, words=SC.words, chat=SC.chat, audio_rms_db=SC.rms_db, params={}
    )
    base.update(kw)
    return SignalContext(**base)  # type: ignore[arg-type]


def top_peaks(values: np.ndarray, k: int, sep: int = 60) -> list[int]:  # type: ignore[type-arg]
    order = np.argsort(-values)
    out: list[int] = []
    for i in order:
        if all(abs(int(i) - j) >= sep for j in out):
            out.append(int(i))
        if len(out) == k:
            break
    return sorted(out)


def near(found: list[int], expected: list[int], tol: int) -> bool:
    return all(any(abs(f - e) <= tol for f in found) for e in expected)


# ---------------------------------------------------------------- helpers
def test_bin_counts_and_weights() -> None:
    assert bin_counts([0.1, 0.9, 1.5, 9.9, 10.0, -1], 10).tolist() == [2, 1, 0, 0, 0, 0, 0, 0, 0, 1]
    assert bin_counts([0.5, 0.6], 2, [0.25, 0.5]).tolist() == [0.75, 0.0]
    assert bin_counts([], 3).tolist() == [0, 0, 0]


def test_rolling_helpers() -> None:
    x = np.array([0, 0, 10, 0, 0], dtype=float)
    assert rolling_median(x, 3).tolist() == [0, 0, 0, 0, 0]  # median kills a lone spike
    assert rolling_mean(np.ones(5), 3).tolist() == [1, 1, 1, 1, 1]  # edges don't sag
    assert shift_earlier(np.arange(5.0), 2).tolist() == [2, 3, 4, 0, 0]


def test_robust_spike_flat_series_is_zero_and_spike_is_one() -> None:
    flat = np.full(600, 3.0)
    assert robust_spike(flat, 120, 3).max() == 0
    x = np.full(600, 3.0) + np.random.default_rng(1).normal(0, 0.2, 600)
    x[300] = 20
    s = robust_spike(x, 120, 3)
    assert s[300] > 0.8 and np.median(s) < 0.2
    assert robust_spike(x, 120, 3)[300] < 1.0  # soft: never hard-saturates


# ---------------------------------------------------------------- signals
def test_chat_velocity_peaks_at_moments_after_lag_correction() -> None:
    s = ChatVelocity().extract(ctx())
    assert s is not None and s.values.shape == (SC.n,)
    assert s.values.min() >= 0 and s.values.max() <= 1
    assert near(top_peaks(s.values, 3), SC.moments, tol=6)
    assert s.meta["messages"] == len(SC.chat)


def test_chat_velocity_without_lag_correction_is_late() -> None:
    s = ChatVelocity().extract(ctx(params={"chat_lag_s": 0}))
    assert s is not None
    peaks = top_peaks(s.values, 3)
    assert all(p >= m + 3 for p, m in zip(peaks, SC.moments, strict=True))


def test_chat_signals_absent_or_sparse_chat_returns_none() -> None:
    assert ChatVelocity().extract(ctx(chat=None)) is None
    sparse = [ChatMessage(float(i), "a", "hi") for i in range(10)]
    assert ChatVelocity().extract(ctx(chat=sparse)) is None
    assert EmoteBurst().extract(ctx(chat=None)) is None


def test_emote_classification() -> None:
    def m(text: str, emotes: tuple[str, ...] = ()) -> ChatMessage:
        return ChatMessage(0, "a", text, emotes)

    assert is_spammy(m("KEKW KEKW"), DEFAULT_EMOTES)
    assert is_spammy(m("AAAAAAA"), DEFAULT_EMOTES)
    assert is_spammy(m("😂😂"), DEFAULT_EMOTES)
    assert is_spammy(m("catDance", ("catDance",)), DEFAULT_EMOTES)  # provider-tagged emote
    assert not is_spammy(m("what game is this"), DEFAULT_EMOTES)


def test_emote_burst_peaks_at_moments() -> None:
    s = EmoteBurst().extract(ctx())
    assert s is not None
    assert near(top_peaks(s.values, 3), SC.moments, tol=8)


def test_audio_energy_peaks_at_moments_and_handles_missing() -> None:
    s = AudioEnergy().extract(ctx())
    assert s is not None
    assert near(top_peaks(s.values, 3), SC.moments, tol=4)
    assert AudioEnergy().extract(ctx(audio_rms_db=None)) is None
    short = AudioEnergy().extract(ctx(audio_rms_db=SC.rms_db[:100]))  # padded with silence
    assert short is not None and short.values.shape == (SC.n,)


def test_speech_intensity_peaks_at_moments_and_always_returns() -> None:
    s = SpeechIntensity().extract(ctx())
    assert s is not None
    assert near(top_peaks(s.values, 3), SC.moments, tol=6)
    empty = SpeechIntensity().extract(ctx(words=[]))
    assert empty is not None and empty.values.sum() == 0  # required signal never "absent"


@pytest.mark.parametrize(
    ("word", "hot"),
    [("WHAT!", True), ("no", True), ("insane", True), ("hello", False), ("the", False)],
)
def test_excitement(word: str, hot: bool) -> None:
    assert (excitement(word) >= 0.4) is hot
