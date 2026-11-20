"""Twitch VOD chat replay via Twitch's *unofficial* web GQL endpoint.

There is no official API for VOD chat. This uses the same public endpoint and client id as
the twitch.tv web player. It is behind ENABLE_TWITCH_CHAT_GQL (default off, ADR-0006),
rate-limits itself, never authenticates or works around blocks, and fails soft: any problem
raises ChatUnavailable and the pipeline continues on audio + transcript.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from typing import Any

from clipforge.adapters.chat.common import hash_author, post_json
from clipforge.domain.values import Platform
from clipforge.ports.analysis import ChatMessage, ChatUnavailable
from clipforge.ports.pipeline import ResolvedVideo

GQL_URL = "https://gql.twitch.tv/gql"
WEB_CLIENT_ID = "kimne78kx3ncx6brgo4mv6wki5h1ko"  # public client id of twitch.tv itself
COMMENTS_QUERY_HASH = "b70a3591ff0f4e0313d126c6a1502d79a1c02baebb288227c582044aa76adf6a"


def parse_page(page: Any, salt: str) -> tuple[list[ChatMessage], str | None, bool]:
    """One GQL response → (messages, last cursor, has_next). Pure; unit-tested."""
    try:
        item = page[0] if isinstance(page, list) else page
        if item.get("errors"):
            raise ChatUnavailable(f"twitch gql error: {item['errors'][0].get('message')}")
        comments = item["data"]["video"]["comments"]
    except (KeyError, TypeError, IndexError) as exc:
        raise ChatUnavailable(f"unexpected twitch gql shape: {exc}") from exc
    if comments is None:
        return [], None, False
    msgs: list[ChatMessage] = []
    cursor = None
    for edge in comments.get("edges") or []:
        cursor = edge.get("cursor") or cursor
        node = edge.get("node") or {}
        frags = (node.get("message") or {}).get("fragments") or []
        text = "".join(f.get("text", "") for f in frags).strip()
        emotes = tuple(f.get("text", "").strip() for f in frags if f.get("emote"))
        who = (node.get("commenter") or {}).get("login") or "anon"
        if text or emotes:
            msgs.append(
                ChatMessage(
                    float(node.get("contentOffsetSeconds", 0)), hash_author(salt, who), text, emotes
                )
            )
    has_next = bool((comments.get("pageInfo") or {}).get("hasNextPage"))
    return msgs, cursor, has_next


class TwitchGqlChatReplay:
    platform = Platform.TWITCH

    def __init__(self, salt: str, *, page_delay_s: float = 0.25, max_pages: int = 20000) -> None:
        self.salt = salt
        self.page_delay_s = page_delay_s
        self.max_pages = max_pages

    def _request(self, video_id: str, cursor: str | None) -> Any:
        variables: dict[str, Any] = {"videoID": video_id}
        if cursor:
            variables["cursor"] = cursor
        else:
            variables["contentOffsetSeconds"] = 0
        body = [
            {
                "operationName": "VideoCommentsByOffsetOrCursor",
                "variables": variables,
                "extensions": {"persistedQuery": {"version": 1, "sha256Hash": COMMENTS_QUERY_HASH}},
            }
        ]
        return post_json(GQL_URL, body, {"Client-Id": WEB_CLIENT_ID})

    def fetch_replay(self, video: ResolvedVideo) -> Iterator[ChatMessage]:
        if video.platform is not Platform.TWITCH:
            raise ChatUnavailable("not a twitch video")
        cursor: str | None = None
        last_t = -1.0
        for _ in range(self.max_pages):
            msgs, cursor, has_next = parse_page(self._request(video.external_id, cursor), self.salt)
            for m in msgs:
                if m.t_s >= last_t:  # pages can overlap at the boundary
                    yield m
                    last_t = m.t_s
            if not has_next or cursor is None:
                return
            time.sleep(self.page_delay_s)
        raise ChatUnavailable(f"stopped after {self.max_pages} pages (safety cap)")
