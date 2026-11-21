"""Fakes for M6 ports."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import ClassVar

from clipforge.ports.publish import PublishError, PublishRequest, PublishResult, VideoStats


@dataclass
class FakePublisher:
    platform: ClassVar[str] = "youtube"
    fail: str | None = None  # PublishError message to raise
    crash: bool = False  # raise a non-PipelineError (network blow-up)
    uploads: list[tuple[PublishRequest, bytes, bytes]] = field(default_factory=list)
    views: dict[str, int] = field(default_factory=dict)

    def publish(self, req: PublishRequest, credentials: bytes) -> PublishResult:
        if self.crash:
            raise ConnectionError("socket closed")
        if self.fail:
            raise PublishError(self.fail)
        self.uploads.append((req, credentials, req.path.read_bytes()))
        vid = f"yt{len(self.uploads)}"
        self.views.setdefault(vid, 0)
        return PublishResult(vid, f"https://youtu.be/{vid}")

    def stats(self, external_ids: Sequence[str], credentials: bytes) -> dict[str, VideoStats]:
        return {
            i: VideoStats(self.views[i], likes=1, comments=0)
            for i in external_ids
            if i in self.views
        }
