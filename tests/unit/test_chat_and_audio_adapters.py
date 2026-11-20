"""Chat parsers (no network) and the PyAV loudness analyzer on a generated WAV."""

from __future__ import annotations

import json
import math
import struct
import wave
from pathlib import Path

import numpy as np
import pytest

from clipforge.adapters.audio.pyav_rms import AudioAnalysisFailed, PyAvRmsAnalyzer
from clipforge.adapters.chat.common import hash_author
from clipforge.adapters.chat.twitch_gql import parse_page
from clipforge.adapters.chat.youtube_live_chat import parse_live_chat_line
from clipforge.ports.analysis import ChatUnavailable

FIX = Path(__file__).parent.parent / "fixtures" / "chat"


def test_author_hash_is_stable_salted_and_not_the_name() -> None:
    a = hash_author("ws1", "SomeViewer")
    assert a == hash_author("ws1", "someviewer")  # case-insensitive
    assert a != hash_author("ws2", "SomeViewer")  # salted per workspace
    assert "someviewer" not in a and len(a) == 16


def test_twitch_page_parsing() -> None:
    page = json.loads((FIX / "twitch_gql_page.json").read_text())
    msgs, cursor, has_next = parse_page(page, "salt")
    assert [m.t_s for m in msgs] == [12.0, 15.0]  # empty message dropped
    assert msgs[0].text == "hello chat" and msgs[0].emotes == ()
    assert msgs[1].text == "KEKW KEKW" and msgs[1].emotes == ("KEKW", "KEKW")
    assert cursor == "c3" and has_next is True
    assert msgs[0].author == hash_author("salt", "SomeViewer")


def test_twitch_errors_and_bad_shapes_raise_chat_unavailable() -> None:
    with pytest.raises(ChatUnavailable, match="failed integrity"):
        parse_page([{"errors": [{"message": "failed integrity check"}]}], "s")
    with pytest.raises(ChatUnavailable):
        parse_page({"data": None}, "s")
    assert parse_page([{"data": {"video": {"comments": None}}}], "s") == ([], None, False)


def test_youtube_live_chat_parsing() -> None:
    msgs = []
    for line in (FIX / "youtube_live_chat.jsonl").read_text(encoding="utf-8").splitlines():
        msgs.extend(parse_live_chat_line(line, "salt"))
    assert [(m.t_s, m.text) for m in msgs] == [(4.2, "lets gooo :fire:"), (9.0, "superchat!")]
    assert msgs[0].emotes == (":fire:",)


def _wav(path: Path, chunks: list[tuple[float, float]], rate: int = 16000) -> Path:
    """chunks: (seconds, amplitude 0..1) of a 440 Hz tone."""
    frames = bytearray()
    t0 = 0
    for secs, amp in chunks:
        for i in range(int(secs * rate)):
            v = amp * math.sin(2 * math.pi * 440 * (t0 + i) / rate)
            frames += struct.pack("<h", int(v * 32767))
        t0 += int(secs * rate)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(bytes(frames))
    return path


def test_pyav_rms_per_second_finds_loud_section(tmp_path: Path) -> None:
    wav = _wav(tmp_path / "a.wav", [(5, 0.02), (2, 0.8), (3, 0.02)])
    db = PyAvRmsAnalyzer().rms_db_per_second(wav)
    assert db.shape == (10,)
    assert int(np.argmax(db)) in (5, 6)
    # a sine at amplitude A has RMS A/sqrt(2): 0.8 → ≈ -4.9 dBFS, 0.02 → ≈ -37 dBFS
    assert db[5] == pytest.approx(20 * math.log10(0.8 / math.sqrt(2)), abs=1.0)
    assert db[0] == pytest.approx(20 * math.log10(0.02 / math.sqrt(2)), abs=1.0)


def test_pyav_rms_bad_file_raises(tmp_path: Path) -> None:
    bad = tmp_path / "bad.m4a"
    bad.write_bytes(b"not audio")
    with pytest.raises(AudioAnalysisFailed):
        PyAvRmsAnalyzer().rms_db_per_second(bad)
