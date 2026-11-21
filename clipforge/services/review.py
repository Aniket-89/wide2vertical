"""Review actions on candidates: approve / reject / trim / edit captions, layout, intro.

Every human decision is recorded as a ``feedback_events`` row with a snapshot of what the
scorer believed (score, components, judge verdict). That is the training set for M8.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from clipforge.domain.values import Stage
from clipforge.ports.pipeline import PipelineError
from clipforge.ports.render import CandidateRecord, ClipRecord, RenderPipelineStore
from clipforge.rendering.layout import LAYOUTS
from clipforge.services._common import require_not_retired

STATUSES = ("pending", "approved", "rejected")
MIN_CLIP_S, MAX_CLIP_S = 5.0, 180.0
RENDER_AFFECTING = {
    "trimmed",
    "caption_edited",
    "layout_changed",
    "style_changed",
    "crop_overridden",
    "commentary_set",
}


class ReviewInvalid(PipelineError):
    """The requested edit is invalid; the message is user-facing."""


@dataclass(frozen=True)
class ReviewOutcome:
    candidate: CandidateRecord
    clip: ClipRecord | None
    changes: list[str] = field(default_factory=list)
    final_render_needed: bool = False


def _validate_crop(crop: Mapping[str, Any]) -> None:
    if crop.get("layout") not in LAYOUTS:
        raise ReviewInvalid(f"crop_override.layout must be one of {LAYOUTS}")
    for key in ("cam", "game"):
        r = crop.get(key)
        if r is None:
            continue
        if not (isinstance(r, list | tuple) and len(r) == 4):
            raise ReviewInvalid(f"crop_override.{key} must be [x, y, w, h]")
        x, y, w, h = (float(v) for v in r)
        if not (
            0 <= x < 1
            and 0 <= y < 1
            and 0 < w <= 1
            and 0 < h <= 1
            and x + w <= 1.0001
            and y + h <= 1.0001
        ):
            raise ReviewInvalid(f"crop_override.{key} must be normalised 0..1 inside the frame")


class ReviewService:
    def __init__(self, store: RenderPipelineStore) -> None:
        self.store = store

    def _snapshot(self, c: CandidateRecord) -> dict[str, Any]:
        judge = c.judge or {}
        return {
            "score": c.score,
            "rank": c.rank,
            "duration_s": round(c.end_s - c.start_s, 3),
            "components": {k: v for k, v in c.components.items() if isinstance(v, int | float)},
            "judge_overall": judge.get("overall"),
            "judge_model": judge.get("model"),
        }

    def update(
        self,
        candidate_id: UUID,
        *,
        status: str | None = None,
        start_s: float | None = None,
        end_s: float | None = None,
        caption_text: str | None = None,
        layout: str | None = None,
        caption_style_id: UUID | None = None,
        crop_override: Mapping[str, Any] | None = None,
        commentary_text: str | None = None,
        clear_crop_override: bool = False,
    ) -> ReviewOutcome:
        cand = self.store.get_candidate(candidate_id)
        if cand is None:
            raise ReviewInvalid("candidate not found")
        video = self.store.get_video(cand.video_id)
        if video is None:
            raise ReviewInvalid("video not found")
        if status is not None and status not in STATUSES:
            raise ReviewInvalid(f"status must be one of {STATUSES}")
        if status == "approved":
            require_not_retired(self.store, video, Stage.RENDER)

        edits_clip = (
            any(
                v is not None
                for v in (
                    start_s,
                    end_s,
                    caption_text,
                    layout,
                    caption_style_id,
                    crop_override,
                    commentary_text,
                )
            )
            or clear_crop_override
        )
        clip = self.store.get_clip_for_candidate(cand.id)
        if clip is None and (edits_clip or status == "approved"):
            src = self.store.source_for_video(video)
            clip = self.store.create_clip(cand, src.required_credit_text if src else "")

        changes: list[str] = []
        fields: dict[str, Any] = {}
        if clip is not None and edits_clip:
            new_start = clip.start_s if start_s is None else float(start_s)
            new_end = clip.end_s if end_s is None else float(end_s)
            if (new_start, new_end) != (clip.start_s, clip.end_s):
                if new_start < 0 or new_end <= new_start:
                    raise ReviewInvalid("start_s must be >= 0 and before end_s")
                if not MIN_CLIP_S <= new_end - new_start <= MAX_CLIP_S:
                    raise ReviewInvalid(f"clip length must be {MIN_CLIP_S:g}-{MAX_CLIP_S:g}s")
                if video.duration_s and new_end > video.duration_s + 0.01:
                    raise ReviewInvalid("end_s is past the end of the video")
                fields.update(start_s=round(new_start, 3), end_s=round(new_end, 3))
                self.store.record_feedback(
                    "trimmed",
                    cand.id,
                    clip.id,
                    before=[clip.start_s, clip.end_s],
                    after=[fields["start_s"], fields["end_s"]],
                    **self._snapshot(cand),
                )
                changes.append("trimmed")
            if caption_text is not None:
                text = caption_text.strip()
                if len(text) > 2000:
                    raise ReviewInvalid("caption text is limited to 2000 characters")
                new_override = {"text": text} if text else None
                if new_override != clip.caption_override:
                    fields["caption_override"] = new_override
                    self.store.record_feedback("caption_edited", cand.id, clip.id, length=len(text))
                    changes.append("caption_edited")
            if layout is not None and layout != clip.layout:
                if layout != "auto" and layout not in LAYOUTS:
                    raise ReviewInvalid(f"layout must be 'auto' or one of {LAYOUTS}")
                fields["layout"] = layout
                self.store.record_feedback(
                    "layout_changed", cand.id, clip.id, before=clip.layout, after=layout
                )
                changes.append("layout_changed")
            if caption_style_id is not None and caption_style_id != clip.caption_style_id:
                self.store.get_caption_style(caption_style_id)  # raises if not in workspace
                fields["caption_style_id"] = caption_style_id
                changes.append("style_changed")
            if crop_override is not None or clear_crop_override:
                new_crop = None if clear_crop_override else dict(crop_override or {})
                if new_crop is not None:
                    _validate_crop(new_crop)
                if new_crop != clip.crop_override:
                    fields["crop_override"] = new_crop
                    self.store.record_feedback("crop_overridden", cand.id, clip.id, crop=new_crop)
                    changes.append("crop_overridden")
            if commentary_text is not None and commentary_text.strip() != clip.commentary_text:
                if len(commentary_text) > 200:
                    raise ReviewInvalid("intro/commentary text is limited to 200 characters")
                fields["commentary_text"] = commentary_text.strip()
                self.store.record_feedback(
                    "commentary_set", cand.id, clip.id, has_text=bool(commentary_text.strip())
                )
                changes.append("commentary_set")
            if fields:
                clip = self.store.update_clip(clip.id, **fields)

        if status is not None and status != cand.status:
            self.store.set_candidate_status(cand.id, status)
            kind = {"approved": "approved", "rejected": "rejected", "pending": "reset"}[status]
            self.store.record_feedback(
                kind, cand.id, clip.id if clip else None, **self._snapshot(cand)
            )
            changes.append(kind)

        updated = self.store.get_candidate(cand.id)
        assert updated is not None
        final_needed = updated.status == "approved" and (
            "approved" in changes or bool(RENDER_AFFECTING & set(changes))
        )
        return ReviewOutcome(updated, clip, changes, final_needed)


def reused_content_warnings(clip: ClipRecord) -> list[str]:
    """Spec §2: YouTube may refuse monetisation for reused content even with permission."""
    if clip.commentary_text.strip() or clip.caption_override:
        return []
    return [
        "No added commentary/intro and unedited captions: YouTube may treat this clip as "
        "reused content. Consider adding an intro or commentary overlay."
    ]
