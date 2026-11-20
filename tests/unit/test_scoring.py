"""Combiner, peak picking, windowing, boundary snapping, NMS, end-to-end generation."""

from __future__ import annotations

import numpy as np
import pytest

from clipforge.ports.analysis import CandidateWindow, SignalContext, SignalSeries
from clipforge.ports.pipeline import Word
from clipforge.scoring.combine import combine
from clipforge.scoring.evaluate import evaluate_at_k
from clipforge.scoring.generate import generate_candidates
from clipforge.scoring.nms import iou, nms
from clipforge.scoring.peaks import find_peaks
from clipforge.scoring.windows import WindowParams, make_window, speech_boundaries
from clipforge.signals import default_signals
from tests.scenario import make_scenario


def series(name: str, values: list[float] | np.ndarray) -> SignalSeries:  # type: ignore[type-arg]
    return SignalSeries(name, "1", np.asarray(values, dtype=float))


# ---------------------------------------------------------------- combine
def test_combine_renormalises_over_available_signals() -> None:
    sig = {"audio_energy": series("audio_energy", [1.0] * 10)}
    combined, used = combine(sig, {"chat_velocity": 0.5, "audio_energy": 0.5}, 10, smoothing_s=1)
    assert used == {"audio_energy": 1.0}
    assert combined.max() == pytest.approx(1.0)  # not capped at 0.5 by the missing chat


def test_combine_ignores_zero_weights_and_handles_nothing() -> None:
    sig = {"a": series("a", [1.0] * 5), "b": series("b", [0.0] * 5)}
    combined, used = combine(sig, {"a": 0.0, "b": 1.0}, 5, smoothing_s=1)
    assert used == {"b": 1.0} and combined.max() == 0
    assert combine({}, {"a": 1.0}, 5)[1] == {}


# ---------------------------------------------------------------- peaks
def test_find_peaks_orders_and_separates() -> None:
    x = np.zeros(200)
    x[[20, 25, 100, 180]] = [0.9, 0.95, 0.5, 0.7]
    assert find_peaks(x, min_distance_s=10, percentile=0, min_height=0.1) == [25, 180, 100]


def test_find_peaks_threshold_and_plateau() -> None:
    x = np.zeros(100)
    x[40:45] = 0.8  # plateau → one peak
    x[70] = 0.05  # below min_height
    assert find_peaks(x, min_distance_s=5, percentile=0, min_height=0.1) == [40]


# ---------------------------------------------------------------- windows
def W(text: str, s: float, e: float) -> Word:
    return Word(text, s, e, 1.0)


def test_speech_boundaries_from_punctuation_and_pauses() -> None:
    words = [W("hi", 0, 0.5), W("there.", 0.6, 1.0), W("so", 1.2, 1.4), W("um", 3.0, 3.2)]
    b = speech_boundaries(words, pause_s=0.7)
    assert b == [pytest.approx(1.1), pytest.approx(2.2)]


def test_make_window_snaps_and_pads() -> None:
    p = WindowParams(setup_s=18, payoff_s=9, lead_in_s=1.5, tail_s=1.0, snap_tolerance_s=6)
    start, end = make_window(100, 1000, boundaries=[80.0, 110.5], p=p)
    assert start == pytest.approx(80 - 1.5) and end == pytest.approx(110.5 + 1.0)


@pytest.mark.parametrize("peak", [2.0, 500.0, 998.0])
def test_make_window_respects_length_limits_and_video_edges(peak: float) -> None:
    p = WindowParams()
    start, end = make_window(peak, 1000, boundaries=[], p=p)
    assert 0 <= start < peak <= end <= 1000
    assert p.min_clip_s <= end - start <= p.max_clip_s


def test_make_window_never_snaps_past_the_peak() -> None:
    p = WindowParams(setup_s=5, payoff_s=5, snap_tolerance_s=6)
    start, end = make_window(100, 1000, boundaries=[99.0, 101.0], p=p)
    assert start < 100 < end


# ---------------------------------------------------------------- nms
def cw(start: float, end: float, score: float) -> CandidateWindow:
    return CandidateWindow(start, end, (start + end) / 2, score, {}, "")


def test_iou() -> None:
    assert iou(cw(0, 10, 1), cw(5, 15, 1)) == pytest.approx(5 / 15)
    assert iou(cw(0, 10, 1), cw(10, 20, 1)) == 0


def test_nms_collapses_overlaps_enforces_gap_and_cap() -> None:
    ws = [cw(0, 30, 0.9), cw(5, 35, 0.8), cw(100, 130, 0.7), cw(118, 148, 0.6), cw(300, 330, 0.5)]
    kept = nms(ws, iou_threshold=0.3, min_gap_s=30, max_count=10)
    assert [w.score for w in kept] == [0.9, 0.7, 0.5]
    assert len(nms(ws, iou_threshold=0.3, min_gap_s=30, max_count=2)) == 2


# ---------------------------------------------------------------- end to end
def _signals_for(sc, *, chat: bool = True) -> dict[str, SignalSeries]:  # type: ignore[no-untyped-def]
    ctx = SignalContext(sc.n, sc.words, sc.chat if chat else None, sc.rms_db, {})
    return {s.name: out for s in default_signals() if (out := s.extract(ctx)) is not None}


WEIGHTS = {"chat_velocity": 0.3, "emote_burst": 0.1, "audio_energy": 0.2, "speech_intensity": 0.15}


def test_generate_finds_planted_moments_as_top_candidates() -> None:
    sc = make_scenario()
    ranked, _combined, used = generate_candidates(
        _signals_for(sc), sc.words, sc.n, WEIGHTS, {"max_clips_per_hour": 12}
    )
    assert set(used) == set(WEIGHTS)
    top3 = [(c.start_s, c.end_s) for c in ranked[:3]]
    assert evaluate_at_k(top3, sc.moments, 3, tolerance_s=0).recall == 1.0
    for c in ranked:
        assert 20 <= c.end_s - c.start_s <= 60
        assert c.start_s < c.peak_s < c.end_s
        assert set(c.components) == set(WEIGHTS)
    assert "chat spiked" in ranked[0].reason and "x baseline" in ranked[0].reason


def test_generate_degrades_without_chat() -> None:
    sc = make_scenario()
    ranked, _, used = generate_candidates(_signals_for(sc, chat=False), sc.words, sc.n, WEIGHTS, {})
    assert "chat_velocity" not in used
    top3 = [(c.start_s, c.end_s) for c in ranked[:3]]
    assert evaluate_at_k(top3, sc.moments, 3, tolerance_s=0).recall == 1.0


def test_generate_caps_clips_per_hour() -> None:
    sc = make_scenario(n=3600, moments=tuple(range(200, 3500, 150)))
    ranked, _, _ = generate_candidates(
        _signals_for(sc), sc.words, sc.n, WEIGHTS, {"max_clips_per_hour": 5}
    )
    assert len(ranked) == 5
