"""Caption timing/ASS generation and layout geometry (pure)."""

from __future__ import annotations

import re

import pytest

from clipforge.ports.pipeline import Word
from clipforge.rendering.captions import (
    CaptionStyle,
    ass_color,
    ass_time,
    build_ass,
    clip_words,
    group_lines,
    sanitize,
)
from clipforge.rendering.layout import (
    OUT_H,
    OUT_W,
    Box,
    FrameFaces,
    choose_layout,
    crop_to_aspect,
    facecam_from_face,
)


def W(t: str, s: float, e: float) -> Word:
    return Word(t, s, e, 1.0)


WORDS = [
    W("no", 100.0, 100.3),
    W("way", 100.3, 100.6),
    W("that", 100.7, 100.9),
    W("just", 100.9, 101.1),
    W("happened!", 101.1, 101.6),
    W("okay", 103.0, 103.4),
]


# ---------------------------------------------------------------- captions
def test_ass_color_and_time() -> None:
    assert ass_color("#FFE600") == "&H0000E6FF"
    assert ass_time(0) == "0:00:00.00" and ass_time(3723.456) == "1:02:03.46"
    with pytest.raises(ValueError):
        ass_color("yellow")


def test_sanitize_blocks_override_injection() -> None:
    evil = "{\\p1}m 0 0 l 100 0{\\p0} hi\x07"
    out = sanitize(evil)
    assert "{" not in out and "\\" not in out and "\x07" not in out


def test_clip_words_are_relative_and_scoped() -> None:
    ws = clip_words(WORDS, 100.0, 102.0)
    assert [w.text for w in ws] == ["no", "way", "that", "just", "happened!"]
    assert ws[0].start == 0.0 and ws[-1].end == pytest.approx(1.6)


def test_override_with_same_word_count_keeps_timing() -> None:
    ws = clip_words(WORDS, 100.0, 102.0, "NO WAY THAT JUST HAPPENED")
    assert ws[1].text == "WAY" and ws[1].start == pytest.approx(0.3)


def test_override_with_different_count_is_spread_over_speech() -> None:
    ws = clip_words(WORDS, 100.0, 102.0, "absolutely unbelievable")
    assert len(ws) == 2 and ws[0].start == 0.0
    assert ws[-1].end == pytest.approx(1.6, abs=0.01)
    assert ws[1].start == pytest.approx(ws[0].end)


def test_group_lines_breaks_on_count_chars_sentence_and_pause() -> None:
    lines = group_lines(clip_words(WORDS, 100.0, 104.0), words_per_line=3, max_chars=18)
    assert [[w.text for w in ln] for ln in lines] == [
        ["no", "way", "that"],
        ["just", "happened!"],
        ["okay"],
    ]


def test_build_ass_highlights_one_word_per_event_with_monotonic_times() -> None:
    style = CaptionStyle(font="DejaVu Sans")
    ass = build_ass(
        clip_words(WORDS, 100.0, 104.0), style, 4.0, credit="Clip: @streamer", intro="wait for it"
    )
    assert "PlayResX: 1080" in ass and "PlayResY: 1920" in ass
    captions = [ln for ln in ass.splitlines() if ",Caption," in ln]
    assert len(captions) == 6  # one event per word
    for ln in captions:
        assert ln.count("\\c&H0000E6FF") == 1  # exactly one highlighted word
    first = captions[0]
    assert "{\\c&H0000E6FF\\fscx108\\fscy108}NO{\\r} WAY THAT" in first
    starts = [re.search(r"Dialogue: \d,([^,]+),", ln).group(1) for ln in captions]  # type: ignore[union-attr]
    assert starts == sorted(starts)
    assert ",Credit,,0,0,0,,Clip: @streamer" in ass
    assert ",Intro," in ass and "0:00:03.50" in ass
    assert ",380,1" in ass  # safe margin above platform UI


def test_style_from_spec_ignores_unknown_keys() -> None:
    s = CaptionStyle.from_spec({"size": 80, "nonsense": 1})
    assert s.size == 80


# ---------------------------------------------------------------- layout
def faces(boxes: list[Box | None], w: int = 1920, h: int = 1080) -> FrameFaces:
    return FrameFaces(w, h, [[b] if b else [] for b in boxes])


def test_crop_to_aspect_centres_and_clamps() -> None:
    full = Box(0, 0, 1920, 1080)
    c = crop_to_aspect(full, 9, 16)
    assert (c.w, c.h) == (608, 1080) and c.x == 656
    left = crop_to_aspect(full, 9, 16, cx=10)
    assert left.x == 0


def test_small_stable_face_means_facecam_overlay() -> None:
    face = Box(1600, 780, 120, 120)  # bottom-right webcam
    plan = choose_layout(faces([face] * 8 + [None, None]))
    assert plan.layout == "facecam_top_gameplay_bottom"
    assert plan.cam is not None and plan.cam.x <= face.x and plan.cam.y <= face.y
    assert plan.cam.x + plan.cam.w <= 1920 and plan.cam.y + plan.cam.h <= 1080
    # game crop matches the remaining output band's aspect ratio
    band = OUT_H - plan.cam_out_h
    assert abs(plan.game.w / plan.game.h - OUT_W / band) < 0.02


def test_large_face_means_center_crop_following_face() -> None:
    plan = choose_layout(faces([Box(400, 200, 500, 500)] * 6))
    assert plan.layout == "center_crop" and plan.game.cx == pytest.approx(650, abs=4)


def test_no_face_or_moving_face_means_blur_background() -> None:
    assert choose_layout(faces([None] * 6)).layout == "gameplay_blur_background"
    moving = [Box(100 + i * 300, 100, 100, 100) for i in range(6)]
    assert choose_layout(faces(moving)).layout == "gameplay_blur_background"


def test_requested_facecam_without_face_degrades() -> None:
    plan = choose_layout(faces([None] * 4), requested="facecam_top_gameplay_bottom")
    assert plan.layout == "gameplay_blur_background" and "none found" in plan.reason


def test_manual_override_uses_normalised_rects() -> None:
    ov = {"layout": "facecam_top_gameplay_bottom", "cam": [0.75, 0.0, 0.25, 0.25]}
    plan = choose_layout(faces([None] * 4), override=ov)
    assert plan.reason == "manual override"
    assert plan.cam == Box(1440, 0, 480, 270)


def test_facecam_rect_stays_in_frame() -> None:
    cam = facecam_from_face(Box(1880, 1040, 40, 40), 1920, 1080)
    assert cam.x + cam.w <= 1920 and cam.y + cam.h <= 1080
    assert cam.w % 2 == 0 and cam.h % 2 == 0  # even dims for H.264


# Regression: a static wide shot of a stage (real Twitch VOD) was misread as a facecam.
def test_static_crowd_of_small_faces_is_not_a_facecam() -> None:
    panel = [
        Box(300, 200, 40, 40),
        Box(520, 210, 38, 38),
        Box(900, 205, 42, 42),
        Box(1300, 215, 40, 40),
    ]
    ff = FrameFaces(1920, 1080, [panel] * 8)
    plan = choose_layout(ff)
    assert plan.layout == "gameplay_blur_background" and "crowd" in plan.reason


def test_small_centred_single_face_is_not_a_facecam() -> None:
    ff = faces([Box(930, 500, 60, 60)] * 8)  # e.g. IRL streamer far from camera
    assert choose_layout(ff).layout == "gameplay_blur_background"


def test_corner_webcam_with_tiny_background_faces_is_still_a_facecam() -> None:
    cam_face = Box(1650, 800, 120, 120)
    noise = Box(400, 300, 30, 30)  # a small face in the gameplay, much smaller
    ff = FrameFaces(1920, 1080, [[cam_face, noise]] * 6 + [[cam_face]] * 2)
    assert choose_layout(ff).layout == "facecam_top_gameplay_bottom"
