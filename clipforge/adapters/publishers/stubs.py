"""Instagram / TikTok publishers: stubs behind feature flags (spec M6). Both platforms need an
app review before content can be posted through their APIs; until that's done these refuse
clearly instead of pretending to post."""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from clipforge.ports.publish import (
    PublishNotSupported,
    PublishRequest,
    PublishResult,
    VideoStats,
)


class _Stub:
    platform: ClassVar[str] = ""
    why: ClassVar[str] = ""

    def publish(self, req: PublishRequest, credentials: bytes) -> PublishResult:
        raise PublishNotSupported(f"{self.platform} publishing isn't implemented yet: {self.why}")

    def stats(self, external_ids: Sequence[str], credentials: bytes) -> dict[str, VideoStats]:
        return {}


class InstagramPublisher(_Stub):
    platform: ClassVar[str] = "instagram"
    why: ClassVar[str] = "Reels posting needs a reviewed Meta app (Instagram Graph API)"


class TikTokPublisher(_Stub):
    platform: ClassVar[str] = "tiktok"
    why: ClassVar[str] = "Content Posting API access needs TikTok's app audit"
