"""YouTube live-chat replay via yt-dlp's ``live_chat`` pseudo-subtitle (unofficial; behind
ENABLE_YOUTUBE_CHAT_REPLAY, default off, ADR-0006). Only exists for past livestreams."""

from __future__ import annotations

import json
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from clipforge.adapters.chat.common import hash_author
from clipforge.adapters.proc import ProcError, run, safe_url_arg
from clipforge.domain.values import Platform
from clipforge.ports.analysis import ChatMessage, ChatUnavailable
from clipforge.ports.pipeline import ResolvedVideo


def _runs_text(runs: list[dict[str, Any]]) -> tuple[str, tuple[str, ...]]:
    parts: list[str] = []
    emotes: list[str] = []
    for r in runs:
        if "text" in r:
            parts.append(r["text"])
        elif "emoji" in r:
            e = r["emoji"]
            label = (e.get("shortcuts") or [e.get("emojiId", "")])[0]
            parts.append(label)
            emotes.append(label)
    return "".join(parts).strip(), tuple(emotes)


def parse_live_chat_line(line: str, salt: str) -> list[ChatMessage]:
    """One JSON line of yt-dlp's .live_chat.json → messages. Pure; unit-tested."""
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return []
    replay = obj.get("replayChatItemAction") or {}
    try:
        t_s = int(replay.get("videoOffsetTimeMsec", "-1")) / 1000.0
    except (TypeError, ValueError):
        return []
    if t_s < 0:
        return []
    out: list[ChatMessage] = []
    for action in replay.get("actions") or []:
        item = (action.get("addChatItemAction") or {}).get("item") or {}
        r = item.get("liveChatTextMessageRenderer") or item.get("liveChatPaidMessageRenderer")
        if not r:
            continue
        text, emotes = _runs_text((r.get("message") or {}).get("runs") or [])
        who = (r.get("authorName") or {}).get("simpleText") or "anon"
        if text or emotes:
            out.append(ChatMessage(t_s, hash_author(salt, who), text, emotes))
    return out


class YouTubeLiveChatReplay:
    platform = Platform.YOUTUBE

    def __init__(self, salt: str, timeout_s: float = 1800) -> None:
        self.salt = salt
        self.timeout_s = timeout_s

    def fetch_replay(self, video: ResolvedVideo) -> Iterator[ChatMessage]:
        if video.platform is not Platform.YOUTUBE:
            raise ChatUnavailable("not a youtube video")
        with tempfile.TemporaryDirectory(prefix="clipforge-chat-") as tmp:
            args = [
                sys.executable,
                "-m",
                "yt_dlp",
                "--skip-download",
                "--write-subs",
                "--sub-langs",
                "live_chat",
                "--use-extractors",
                "youtube",
                "--quiet",
                "--no-warnings",
                "-o",
                str(Path(tmp) / "chat"),
                "--",
                safe_url_arg(video.url),
            ]
            try:
                run(args, timeout_s=self.timeout_s)
            except ProcError as exc:
                raise ChatUnavailable(f"yt-dlp live_chat failed: {exc}") from exc
            files = list(Path(tmp).glob("*.live_chat.json"))
            if not files:
                raise ChatUnavailable("no live chat replay for this video (not a past livestream?)")
            msgs: list[ChatMessage] = []
            with files[0].open(encoding="utf-8") as f:
                for line in f:
                    msgs.extend(parse_live_chat_line(line, self.salt))
        yield from sorted(msgs, key=lambda m: m.t_s)
