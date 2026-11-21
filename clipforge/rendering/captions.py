"""Word-by-word highlighted captions as ASS (Advanced SubStation Alpha), from Whisper word
timestamps. Pure: text in, text out. ffmpeg's ``ass`` filter burns the result in."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from clipforge.ports.pipeline import Word

_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SENTENCE_END = (".", "!", "?", "…")


@dataclass(frozen=True, slots=True)
class CaptionStyle:
    font: str = "Montserrat ExtraBold"
    size: int = 72
    primary: str = "#FFFFFF"
    highlight: str = "#FFE600"
    outline: str = "#000000"
    outline_px: int = 6
    position: str = "lower_third"  # lower_third | middle | top
    safe_margin_bottom_px: int = 380  # keeps captions above TikTok/Reels/Shorts UI
    words_per_line: int = 3
    max_line_chars: int = 18
    uppercase: bool = True

    @classmethod
    def from_spec(cls, spec: Mapping[str, Any] | None) -> CaptionStyle:
        fields = cls.__dataclass_fields__
        known = {k: v for k, v in (spec or {}).items() if k in fields}
        return cls(**known)


def ass_color(hex_rgb: str) -> str:
    """'#RRGGBB' → ASS '&H00BBGGRR' (ASS is BGR with a leading alpha byte)."""
    h = hex_rgb.lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", h):
        raise ValueError(f"bad colour {hex_rgb!r}")
    return f"&H00{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def sanitize(text: str) -> str:
    """Transcript text is third-party data: strip ASS override braces/backslashes and control
    characters so a transcript can't inject drawing or styling commands."""
    return _CONTROL.sub(" ", text).replace("{", "(").replace("}", ")").replace("\\", "/").strip()


def ass_time(t: float) -> str:
    t = max(0.0, t)
    cs = round(t * 100)
    h, rem = divmod(cs, 360000)
    m, rem = divmod(rem, 6000)
    s, c = divmod(rem, 100)
    return f"{h}:{m:02d}:{s:02d}.{c:02d}"


def clip_words(
    words: Sequence[Word], start_s: float, end_s: float, override_text: str | None = None
) -> list[Word]:
    """Words inside [start, end), re-timed relative to the clip start.

    ``override_text`` is the reviewer's edited caption. If it has the same number of words,
    the original timings are kept; otherwise the edited words are spread over the original
    speech span in proportion to their length.
    """
    dur = end_s - start_s
    inside = [
        Word(w.text, max(0.0, w.start - start_s), min(dur, w.end - start_s), w.prob)
        for w in words
        if start_s <= w.start < end_s
    ]
    if override_text is None:
        return [w for w in inside if w.text.strip()]
    tokens = override_text.split()
    if not tokens:
        return []
    if len(tokens) == len(inside):
        return [Word(t, w.start, w.end, w.prob) for t, w in zip(tokens, inside, strict=True)]
    span_start = inside[0].start if inside else 0.0
    span_end = inside[-1].end if inside else dur
    if span_end - span_start < 0.5 * len(tokens) * 0.15:  # too little speech: use whole clip
        span_start, span_end = 0.0, dur
    weights = [len(t) + 1 for t in tokens]
    total = sum(weights)
    out, t = [], span_start
    for tok, wgt in zip(tokens, weights, strict=True):
        d = (span_end - span_start) * wgt / total
        out.append(Word(tok, round(t, 3), round(t + d, 3), 1.0))
        t += d
    return out


def group_lines(words: Sequence[Word], words_per_line: int, max_chars: int) -> list[list[Word]]:
    """Short punchy lines: break on word count, character count, sentence ends and pauses."""
    lines: list[list[Word]] = []
    cur: list[Word] = []
    for w in words:
        cur_chars = sum(len(x.text) + 1 for x in cur)
        gap = (w.start - cur[-1].end) if cur else 0.0
        if cur and (
            len(cur) >= words_per_line
            or cur_chars + len(w.text) > max_chars
            or cur[-1].text.endswith(_SENTENCE_END)
            or gap > 0.8
        ):
            lines.append(cur)
            cur = []
        cur.append(w)
    if cur:
        lines.append(cur)
    return lines


def _alignment(position: str) -> int:
    return {"lower_third": 2, "middle": 5, "top": 8}.get(position, 2)


def build_ass(
    words: Sequence[Word],
    style: CaptionStyle,
    duration_s: float,
    *,
    width: int = 1080,
    height: int = 1920,
    credit: str = "",
    intro: str = "",
) -> str:
    """ASS document: one Dialogue event per spoken word, showing its line with that word
    highlighted. Plus an optional credit line (whole clip) and intro text (first ~3.5 s)."""
    scale = height / 1920
    size = round(style.size * scale)
    margin_v = round(
        (style.safe_margin_bottom_px if style.position == "lower_third" else 120) * scale
    )
    prim, hi, out = ass_color(style.primary), ass_color(style.highlight), ass_color(style.outline)
    font = sanitize(style.font).replace(",", " ")
    header = f"""[Script Info]
ScriptType: v4.00+
PlayResX: {width}
PlayResY: {height}
WrapStyle: 2
ScaledBorderAndShadow: yes

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Caption,{font},{size},{prim},{hi},{out},&H80000000,-1,0,0,0,100,100,0,0,1,{round(style.outline_px * scale)},2,{_alignment(style.position)},60,60,{margin_v},1
Style: Credit,{font},{round(34 * scale)},&H00FFFFFF,&H00FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,{max(1, round(3 * scale))},1,7,40,40,{round(60 * scale)},1
Style: Intro,{font},{round(64 * scale)},&H00FFFFFF,&H00FFFFFF,&H00000000,&HA0000000,-1,0,0,0,100,100,0,0,3,{round(18 * scale)},0,8,80,80,{round(260 * scale)},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""
    events: list[str] = []

    def ev(start: float, end: float, style_name: str, text: str, layer: int = 0) -> None:
        if end - start >= 0.01:
            events.append(
                f"Dialogue: {layer},{ass_time(start)},{ass_time(end)},{style_name},,0,0,0,,{text}"
            )

    def fmt(t: str) -> str:
        t = sanitize(t)
        return t.upper() if style.uppercase else t

    lines = group_lines(words, style.words_per_line, style.max_line_chars)
    for li, line in enumerate(lines):
        next_line_start = lines[li + 1][0].start if li + 1 < len(lines) else duration_s
        line_end = min(next_line_start, line[-1].end + 0.35, duration_s)
        for wi, w in enumerate(line):
            start = line[0].start if wi == 0 else w.start
            end = line[wi + 1].start if wi + 1 < len(line) else line_end
            parts = [
                f"{{\\c{hi}\\fscx108\\fscy108}}{fmt(x.text)}{{\\r}}" if j == wi else fmt(x.text)
                for j, x in enumerate(line)
            ]
            ev(start, max(end, start + 0.05), "Caption", " ".join(parts), layer=1)
    if credit.strip():
        ev(0.0, duration_s, "Credit", sanitize(credit))
    if intro.strip():
        ev(0.0, min(3.5, duration_s), "Intro", sanitize(intro), layer=2)
    return header + "\n".join(events) + ("\n" if events else "")
