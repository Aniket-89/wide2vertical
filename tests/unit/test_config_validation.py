"""Validation of user-edited scoring profiles and caption styles (M5 settings screens)."""

from __future__ import annotations

from typing import Any

import pytest

from clipforge.rendering.captions import CaptionStyle, validate_style_spec
from clipforge.scoring.profile import validate_profile


def test_valid_profile_is_cleaned_to_floats() -> None:
    w, p = validate_profile(
        {"audio_energy": 1, "chat_velocity": 0.5}, {"min_clip_s": 15, "strict": True}
    )
    assert w == {"audio_energy": 1.0, "chat_velocity": 0.5}
    assert p == {"min_clip_s": 15.0, "strict": True}


@pytest.mark.parametrize(
    ("weights", "params", "match"),
    [
        ({}, {}, "1-20 entries"),
        ({"audio_energy": 0}, {}, "at least one weight"),
        ({"audio_energy": -1}, {}, "between 0"),
        ({"audio_energy": float("nan")}, {}, "finite"),
        ({"audio_energy": True}, {}, "finite"),
        ({"Audio-Energy": 1}, {}, "lower_snake_case"),
        ({"audio_energy": 1}, {"min_clip_s": 60, "max_clip_s": 30}, "min_clip_s"),
        ({"audio_energy": 1}, {"max_clip_s": 600}, "max_clip_s"),
        ({"audio_energy": 1}, {"max_clips_per_hour": 1e6}, "max_clips_per_hour"),
        ({"audio_energy": 1}, {"smoothing_s": "3"}, "finite"),
    ],
)
def test_bad_profiles_are_refused(weights: Any, params: Any, match: str) -> None:
    with pytest.raises(ValueError, match=match):
        validate_profile(weights, params)


def test_valid_style_round_trips_into_caption_style() -> None:
    spec = validate_style_spec(
        {"font": "Montserrat ExtraBold", "highlight": "00ff88", "size": 64, "position": "top"}
    )
    assert spec["highlight"] == "#00FF88"
    style = CaptionStyle.from_spec(spec)
    assert style.size == 64 and style.position == "top"


@pytest.mark.parametrize(
    ("spec", "match"),
    [
        ({"colour": "#fff"}, "unknown"),
        ({"font": "Arial,0,0{\\p1}"}, "font"),  # would inject into the ASS Style line
        ({"primary": "red"}, "bad colour"),
        ({"size": 5}, "size"),
        ({"size": 72.5}, "size"),
        ({"words_per_line": True}, "words_per_line"),
        ({"position": "bottom"}, "position"),
        ({"uppercase": "yes"}, "uppercase"),
    ],
)
def test_bad_styles_are_refused(spec: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        validate_style_spec(spec)
