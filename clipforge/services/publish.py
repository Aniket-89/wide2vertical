"""M6 use case: schedule, upload and track published clips.

Policy (spec §2, §M6): a clip is published only when its candidate is approved, its source is
known and not retired, and a final render exists. Publishing is always an explicit request,
except autopublish: with ENABLE_AUTOPUBLISH on, an approved clip from an own-channel source
(owner decision, ADR-0038) is scheduled when its final render completes.

The database is the scheduling queue: publications sit in ``scheduled`` until due; a
scheduler tick claims them with compare-and-set (``scheduled -> queued``) and enqueues one
upload job each. The upload claims ``queued -> uploading``, so a retried or duplicated job
can never upload twice.
"""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

import structlog

from clipforge.domain.values import BlobKey, ClipPolicy, Stage
from clipforge.ports.infra import BlobStore, Clock
from clipforge.ports.pipeline import PipelineError, SourceRecord
from clipforge.ports.publish import (
    PRIVACY,
    PublicationRecord,
    Publisher,
    PublishError,
    PublishPipelineStore,
    PublishRefused,
    PublishRequest,
    PublishResult,
    SecretBox,
)
from clipforge.ports.render import CandidateRecord, ClipRecord, RenderRecord
from clipforge.services._common import SourceNotAllowed, require_not_retired
from clipforge.services.render import export_description

log = structlog.get_logger(__name__)

AUTOPUBLISH_POLICIES = frozenset({ClipPolicy.OWN_CHANNEL})  # owner decision 2026-11-21
TITLE_MAX = 100  # YouTube limits
DESCRIPTION_MAX = 5000
_ANGLES = re.compile(r"[<>]")  # YouTube rejects angle brackets in titles and descriptions


@dataclass(frozen=True)
class PublishConfig:
    autopublish: bool = False
    stats_window_days: int = 30


@dataclass(frozen=True)
class Publishable:
    clip: ClipRecord
    candidate: CandidateRecord
    source: SourceRecord
    final: RenderRecord


def clean_title(title: str) -> str:
    t = " ".join(_ANGLES.sub("", title).split())
    return t[:TITLE_MAX].rstrip()


def clean_description(description: str, credit: str) -> str:
    """Strip characters YouTube refuses and always keep the source's required credit."""
    body = _ANGLES.sub("", description).strip()
    credit = _ANGLES.sub("", credit).strip()[:500]
    if not credit:
        return body[:DESCRIPTION_MAX].rstrip()
    if credit in body and len(body) <= DESCRIPTION_MAX:
        return body
    body = body.replace(credit, "").strip()  # re-append it last so trimming never cuts it
    suffix = f"\n\n{credit}" if body else credit
    return body[: DESCRIPTION_MAX - len(suffix)].rstrip() + suffix


class PublishService:
    def __init__(
        self,
        store: PublishPipelineStore,
        publishers: Mapping[str, Publisher],
        secrets: SecretBox | None,
        blobs: BlobStore,
        clock: Clock,
        cfg: PublishConfig | None = None,
    ) -> None:
        self.store = store
        self.publishers = publishers
        self.secrets = secrets
        self.blobs = blobs
        self.clock = clock
        self.cfg = cfg or PublishConfig()

    # ------------------------------------------------------------ policy
    def check(self, clip_id: UUID) -> Publishable:
        """Everything that must hold to publish. Re-run right before uploading too: a
        takedown between scheduling and the upload must stop it."""
        clip = self.store.get_clip(clip_id)
        if clip is None:  # retiring a source soft-deletes (hides) its clips
            raise PublishRefused("clip not found (deleted, or hidden by a source takedown)")
        cand = self.store.get_candidate(clip.candidate_id)
        if cand is None or cand.status != "approved":
            raise PublishRefused("approve the clip before publishing it")
        video = self.store.get_video(clip.video_id)
        if video is None:
            raise PublishRefused("video not found")
        try:
            require_not_retired(self.store, video, Stage.PUBLISH)
        except SourceNotAllowed as exc:
            raise PublishRefused(exc.reason) from exc
        src = self.store.source_for_video(video)
        if src is None or src.clip_policy == ClipPolicy.UNKNOWN:
            # An override lets you *process* a video; posting needs a recorded permission.
            raise PublishRefused(
                "add this channel to Sources with its clip permission before publishing"
            )
        final = self.store.latest_render(clip.id, "final")
        if final is None or not final.blob_key:
            raise PublishRefused("the final render isn't ready yet")
        return Publishable(clip, cand, src, final)

    # ------------------------------------------------------------ schedule
    def schedule(
        self,
        clip_id: UUID,
        target_id: UUID,
        *,
        title: str | None = None,
        description: str | None = None,
        privacy: str = "private",
        at: datetime | None = None,
        auto: bool = False,
    ) -> PublicationRecord:
        p = self.check(clip_id)
        target = self.store.get_target(target_id)
        if target is None:
            raise PublishRefused("publish target not found")
        if not target.enabled:
            raise PublishRefused(f"{target.account_label} is disabled")
        if target.platform not in self.publishers:
            raise PublishRefused(f"publishing to {target.platform} is not enabled")
        if privacy not in PRIVACY:
            raise PublishRefused(f"privacy must be one of {', '.join(PRIVACY)}")

        suggested = p.candidate.title_suggestions[0] if p.candidate.title_suggestions else ""
        final_title = clean_title(title if title is not None else suggested)
        if not final_title:
            raise PublishRefused("give the post a title")
        body = (
            description
            if description is not None
            else export_description("", p.candidate.hook_text, p.clip.credit_text)
        )
        final_desc = clean_description(body, p.clip.credit_text)
        when = at or self.clock.now()

        # One publication per (clip, target, final render): a double-click or a repeated
        # autopublish can't post twice. A new final render (after an edit) gets a new key.
        key = hashlib.sha256(f"{clip_id}:{target_id}:{p.final.render_hash}".encode()).hexdigest()
        fields: dict[str, Any] = {
            "title": final_title,
            "description": final_desc,
            "privacy": privacy,
            "scheduled_at": when,
            "error": None,
        }
        existing = self.store.find_publication(key)
        if existing is not None:
            if existing.status in ("failed", "cancelled"):
                self.store.transition(existing.id, ("failed", "cancelled"), "scheduled", **fields)
            elif existing.status == "scheduled":
                self.store.transition(existing.id, ("scheduled",), "scheduled", **fields)
            pub = self.store.get_publication(existing.id)
            assert pub is not None
        else:
            pub = self.store.create_publication(
                clip_id,
                target_id,
                title=final_title,
                description=final_desc,
                privacy=privacy,
                scheduled_at=when,
                idempotency_key=key,
            )
        self.store.record_feedback(
            "publish_scheduled",
            p.candidate.id,
            clip_id,
            publication_id=str(pub.id),
            platform=target.platform,
            privacy=privacy,
            auto=auto,
        )
        self.store.audit(
            "publication.scheduled", "publication", pub.id, platform=target.platform, auto=auto
        )
        return pub

    def cancel(self, pub_id: UUID) -> bool:
        ok = self.store.transition(pub_id, ("scheduled", "queued"), "cancelled")
        if ok:
            self.store.audit("publication.cancelled", "publication", pub_id)
        return ok

    def maybe_autopublish(self, clip_id: UUID) -> PublicationRecord | None:
        """Called when a final render completes. Off unless the flag is on and the source's
        policy is in AUTOPUBLISH_POLICIES; needs a default YouTube target."""
        if not self.cfg.autopublish:
            return None
        try:
            p = self.check(clip_id)
        except PublishRefused:
            return None
        if p.source.clip_policy not in AUTOPUBLISH_POLICIES:
            return None
        target = self.store.default_target("youtube")
        if target is None or not target.enabled or target.platform not in self.publishers:
            return None
        try:
            return self.schedule(clip_id, target.id, auto=True)
        except PipelineError as exc:  # e.g. no title suggestion: leave it for a human
            log.info("autopublish_skipped", clip_id=str(clip_id), reason=str(exc)[:200])
            return None

    # ------------------------------------------------------------ scheduler tick
    def claim_if_due(self, pub: PublicationRecord) -> bool:
        """For "publish now": claim it right away instead of waiting for the next tick."""
        due = pub.scheduled_at is None or pub.scheduled_at <= self.clock.now()
        return due and self.store.transition(pub.id, ("scheduled",), "queued")

    def claim_due(self) -> list[UUID]:
        """Due publications moved ``scheduled -> queued``; the caller enqueues uploads."""
        claimed = []
        for pub in self.store.due_publications(self.clock.now()):
            if self.store.transition(pub.id, ("scheduled",), "queued"):
                claimed.append(pub.id)
        return claimed

    # ------------------------------------------------------------ upload
    def run(self, pub_id: UUID) -> PublicationRecord | None:
        """Upload one publication. Never raises for expected failures: the row ends in
        ``failed`` with the reason, so a retry can't double-post."""
        if not self.store.transition(pub_id, ("queued", "scheduled"), "uploading"):
            return self.store.get_publication(pub_id)  # someone else has it, or it's done
        pub = self.store.get_publication(pub_id)
        assert pub is not None
        try:
            result = self._upload(pub)
        except Exception as exc:
            msg = str(exc) if isinstance(exc, PipelineError) else f"{type(exc).__name__}"
            self.store.transition(pub_id, ("uploading",), "failed", error=msg[:1000])
            self.store.audit("publication.failed", "publication", pub_id, error=msg[:500])
            log.warning("publish_failed", publication_id=str(pub_id), error=msg[:300])
            if not isinstance(exc, PipelineError):
                raise PublishError(msg) from exc
            return self.store.get_publication(pub_id)
        now = self.clock.now()
        self.store.transition(
            pub_id,
            ("uploading",),
            "published",
            external_id=result.external_id,
            published_at=now,
        )
        clip = self.store.get_clip(pub.clip_id)
        self.store.record_feedback(
            "published",
            clip.candidate_id if clip else None,
            pub.clip_id,
            publication_id=str(pub_id),
            external_id=result.external_id,
            url=result.url,
            privacy=pub.privacy,
        )
        self.store.record_usage(
            "publish", 1, "posts", clip.video_id if clip else None, publication=str(pub_id)
        )
        self.store.audit("publication.published", "publication", pub_id, url=result.url)
        return self.store.get_publication(pub_id)

    def _upload(self, pub: PublicationRecord) -> PublishResult:
        p = self.check(pub.clip_id)
        target = self.store.get_target(pub.target_id)
        if target is None or not target.enabled:
            raise PublishRefused("publish target missing or disabled")
        publisher = self.publishers.get(target.platform)
        if publisher is None:
            raise PublishRefused(f"publishing to {target.platform} is not enabled")
        creds = self._credentials(target.secret_ref)
        with self.blobs.open_local(BlobKey(p.final.blob_key or "")) as path:
            req = PublishRequest(path, pub.title, pub.description, pub.privacy)
            return publisher.publish(req, creds)

    def _credentials(self, ref: str | None) -> bytes:
        if not ref:
            return b""
        if self.secrets is None:
            raise PublishError("CLIPFORGE_SECRET_KEY is not set, so credentials can't be opened")
        return self.secrets.open(self.store.workspace_id, ref)

    # ------------------------------------------------------------ stats pullback
    def refresh_stats(self) -> int:
        """Pull current counters for recent posts into feedback_events (the M8 dataset)."""
        since = self.clock.now() - timedelta(days=self.cfg.stats_window_days)
        by_target: dict[UUID, list[PublicationRecord]] = defaultdict(list)
        for pub in self.store.published_since(since):
            if pub.external_id:
                by_target[pub.target_id].append(pub)
        written = 0
        for target_id, pubs in by_target.items():
            target = self.store.get_target(target_id)
            publisher = self.publishers.get(target.platform) if target else None
            if target is None or publisher is None:
                continue
            try:
                stats = publisher.stats(
                    [p.external_id for p in pubs if p.external_id],
                    self._credentials(target.secret_ref),
                )
            except PipelineError as exc:
                log.warning("stats_failed", target_id=str(target_id), error=str(exc)[:300])
                continue
            for pub in pubs:
                s = stats.get(pub.external_id or "")
                if s is None:
                    continue
                clip = self.store.get_clip(pub.clip_id)
                age_h = (
                    round((self.clock.now() - pub.published_at).total_seconds() / 3600, 1)
                    if pub.published_at
                    else None
                )
                self.store.record_feedback(
                    "performance",
                    clip.candidate_id if clip else None,
                    pub.clip_id,
                    publication_id=str(pub.id),
                    external_id=pub.external_id,
                    views=s.views,
                    likes=s.likes,
                    comments=s.comments,
                    age_hours=age_h,
                    **dict(s.extra),
                )
                written += 1
        return written
