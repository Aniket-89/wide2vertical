"""RenderPipeline + ReviewService with fakes: idempotency, caching, failures, feedback."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest

from clipforge.adapters.blobstore.local import LocalBlobStore
from clipforge.adapters.eventbus.in_process import InProcessEventBus
from clipforge.adapters.fakes.pipeline import (
    FakeFacecamDetector,
    FakeRenderer,
    FakeSectionFetcher,
    InMemoryRenderStore,
)
from clipforge.domain.ids import new_id
from clipforge.domain.values import BlobKey, ClipPolicy, Platform, WorkspaceId
from clipforge.ports.analysis import CandidateWindow, ScoringProfileRecord
from clipforge.ports.pipeline import FetchFailed, ResolvedVideo, Transcript, Word
from clipforge.ports.render import RenderFailed
from clipforge.rendering.layout import Box, FrameFaces
from clipforge.services._common import SourceNotAllowed
from clipforge.services.render import RenderPipeline, export_description
from clipforge.services.review import ReviewInvalid, ReviewService, reused_content_warnings

WS = WorkspaceId(UUID("01936f00-0000-7000-8000-000000000001"))
WORDS = tuple(Word(f"w{i}", 100 + i * 0.5, 100.4 + i * 0.5, 1.0) for i in range(80))


class Rig:
    def __init__(
        self,
        tmp: Path,
        *,
        faces: FrameFaces | None = None,
        align_s: float = 0.0,
        fetch_fail: bool = False,
        render_fail: bool = False,
    ) -> None:
        prof = ScoringProfileRecord(new_id(), "default", 1, {}, {})
        self.store = InMemoryRenderStore(WS, profiles=[prof])
        self.src = self.store.add_source(
            Platform.TWITCH, "streamer", ClipPolicy.CAMPAIGN, credit="Clip: twitch.tv/streamer"
        )
        rv = ResolvedVideo(
            Platform.TWITCH, "https://www.twitch.tv/videos/42", "42", "streamer", None, "t", 3600.0
        )
        self.video, _ = self.store.upsert_video(rv, self.src.id, False)
        self.store.save_transcript(self.video.id, "h", "c", Transcript("en", 3600, "m", WORDS))
        self.store.replace_candidates(
            self.video.id,
            prof.id,
            [
                CandidateWindow(100.0, 128.0, 120.0, 0.9, {"audio_energy": 0.8}, "loud"),
                CandidateWindow(500.0, 530.0, 520.0, 0.6, {"audio_energy": 0.5}, "meh"),
            ],
        )
        self.cands = self.store.list_candidates(self.video.id)
        self.blobs = LocalBlobStore(tmp / "blobs")
        self.sections = FakeSectionFetcher(fail=fetch_fail, align_s=align_s)
        self.detector = FakeFacecamDetector(faces)
        self.renderer = FakeRenderer(fail=render_fail)
        self.pipe = RenderPipeline(
            self.store, self.sections, self.detector, self.renderer, self.blobs, InProcessEventBus()
        )
        self.review = ReviewService(self.store)


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


# ---------------------------------------------------------------- render pipeline
def test_preview_render_end_to_end(rig: Rig) -> None:
    clip = rig.pipe.ensure_clip(rig.cands[0].id)
    assert clip.credit_text == "Clip: twitch.tv/streamer" and clip.layout == "auto"
    out = rig.pipe.render(clip.id, "preview")
    spec = rig.renderer.specs[-1]
    assert (spec.out_w, spec.out_h, spec.quality) == (540, 960, "preview")
    assert spec.duration_s == pytest.approx(28.0)
    assert rig.sections.calls[0] == (99.0, 129.0, 720)  # ±1 s padding, 720p for previews
    assert "Clip: twitch.tv/streamer" in spec.ass and ",Caption," in spec.ass
    assert out.render.status == "succeeded" and not out.cached
    assert rig.blobs.exists(BlobKey(out.render.blob_key or ""))
    kinds = {u["kind"] for u in rig.store.usage}
    assert {"render_seconds", "storage_bytes"} <= kinds


def test_hls_alignment_offset_is_respected(tmp_path: Path) -> None:
    rig = Rig(tmp_path, align_s=4.5)  # HLS section starts 4.5 s before the padded start
    clip = rig.pipe.ensure_clip(rig.cands[0].id)
    rig.pipe.render(clip.id, "preview")
    assert rig.renderer.specs[-1].offset_s == pytest.approx(1.0 + 4.5)


def test_render_is_idempotent_and_section_cached(rig: Rig) -> None:
    clip = rig.pipe.ensure_clip(rig.cands[0].id)
    first = rig.pipe.render(clip.id, "preview")
    second = rig.pipe.render(clip.id, "preview")
    assert second.cached and second.render.id == first.render.id
    assert len(rig.renderer.specs) == 1
    rig.pipe.render(clip.id, "preview", force=True)
    assert len(rig.renderer.specs) == 2 and len(rig.sections.calls) == 1  # section reused


def test_any_visible_change_rerenders(rig: Rig) -> None:
    clip = rig.pipe.ensure_clip(rig.cands[0].id)
    rig.pipe.render(clip.id, "preview")
    rig.store.update_clip(clip.id, caption_override={"text": "brand new caption"})
    out = rig.pipe.render(clip.id, "preview")
    assert not out.cached and "BRAND" in rig.renderer.specs[-1].ass


def test_final_render_is_full_size(rig: Rig) -> None:
    clip = rig.pipe.ensure_clip(rig.cands[0].id)
    rig.pipe.render(clip.id, "final")
    spec = rig.renderer.specs[-1]
    assert (spec.out_w, spec.out_h, spec.quality) == (1080, 1920, "final")
    assert rig.sections.calls[-1][2] == 1080


def test_facecam_detected_layout_reaches_renderer(tmp_path: Path) -> None:
    faces = FrameFaces(1920, 1080, [[Box(1650, 820, 110, 110)]] * 8)
    rig = Rig(tmp_path, faces=faces)
    clip = rig.pipe.ensure_clip(rig.cands[0].id)
    out = rig.pipe.render(clip.id, "preview")
    assert out.layout == "facecam_top_gameplay_bottom"
    assert rig.renderer.specs[-1].plan.cam is not None


def test_source_crop_override_is_used(rig: Rig) -> None:
    rig.store.source_crops[rig.src.id] = {
        "layout": "facecam_top_gameplay_bottom",
        "cam": [0.0, 0.0, 0.3, 0.3],
    }
    clip = rig.pipe.ensure_clip(rig.cands[0].id)
    out = rig.pipe.render(clip.id, "preview")
    assert out.layout_reason == "manual override"


def test_ffmpeg_crash_marks_render_failed_and_audits(tmp_path: Path) -> None:
    rig = Rig(tmp_path, render_fail=True)
    clip = rig.pipe.ensure_clip(rig.cands[0].id)
    with pytest.raises(RenderFailed):
        rig.pipe.render(clip.id, "preview")
    assert [r.status for r in rig.store.renders.values()] == ["failed"]
    assert rig.store.audits[-1]["action"] == "render.failed"


def test_fetch_failure_marks_render_failed(tmp_path: Path) -> None:
    rig = Rig(tmp_path, fetch_fail=True)
    clip = rig.pipe.ensure_clip(rig.cands[0].id)
    with pytest.raises(FetchFailed):
        rig.pipe.render(clip.id, "preview")
    assert [r.status for r in rig.store.renders.values()] == ["failed"]


def test_takedown_blocks_render(rig: Rig) -> None:
    clip = rig.pipe.ensure_clip(rig.cands[0].id)
    rig.store.retire(rig.src.id)
    with pytest.raises(SourceNotAllowed):
        rig.pipe.render(clip.id, "preview")


def test_preview_top_skips_rejected_and_isolates_failures(rig: Rig) -> None:
    rig.store.set_candidate_status(rig.cands[1].id, "rejected")
    outs = rig.pipe.preview_top(rig.video.id, 5)
    assert len(outs) == 1


# ---------------------------------------------------------------- review
def test_approve_creates_clip_and_records_snapshot(rig: Rig) -> None:
    out = rig.review.update(rig.cands[0].id, status="approved")
    assert out.candidate.status == "approved" and out.clip is not None
    assert out.final_render_needed and out.changes == ["approved"]
    fb = rig.store.feedback[-1]
    assert fb["kind"] == "approved" and fb["score"] == 0.9 and "components" in fb


def test_reject_records_feedback_without_render(rig: Rig) -> None:
    out = rig.review.update(rig.cands[1].id, status="rejected")
    assert not out.final_render_needed and out.clip is None
    assert rig.store.feedback[-1]["kind"] == "rejected"


def test_trim_records_before_after_and_rerenders_approved_clip(rig: Rig) -> None:
    rig.review.update(rig.cands[0].id, status="approved")
    out = rig.review.update(rig.cands[0].id, start_s=102.0, end_s=126.0)
    assert out.changes == ["trimmed"] and out.final_render_needed
    fb = next(f for f in rig.store.feedback if f["kind"] == "trimmed")
    assert fb["before"] == [100.0, 128.0] and fb["after"] == [102.0, 126.0]


def test_editing_a_pending_clip_does_not_render_final(rig: Rig) -> None:
    out = rig.review.update(rig.cands[0].id, caption_text="custom words here")
    assert out.changes == ["caption_edited"] and not out.final_render_needed
    assert out.clip is not None and out.clip.caption_override == {"text": "custom words here"}


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"start_s": 120.0, "end_s": 110.0}, "before end_s"),
        ({"start_s": 100.0, "end_s": 102.0}, "clip length"),
        ({"start_s": 3590.0, "end_s": 3700.0}, "past the end"),
        ({"layout": "sideways"}, "layout must be"),
        ({"status": "maybe"}, "status must be"),
        ({"crop_override": {"layout": "center_crop", "game": [0.5, 0, 0.8, 1]}}, "normalised"),
    ],
)
def test_invalid_edits_are_refused(rig: Rig, kwargs: dict[str, object], match: str) -> None:
    with pytest.raises(ReviewInvalid, match=match):
        rig.review.update(rig.cands[0].id, **kwargs)  # type: ignore[arg-type]


def test_approve_is_blocked_by_takedown(rig: Rig) -> None:
    rig.store.retire(rig.src.id)
    with pytest.raises(SourceNotAllowed):
        rig.review.update(rig.cands[0].id, status="approved")


def test_reused_content_warning_and_description(rig: Rig) -> None:
    out = rig.review.update(rig.cands[0].id, status="approved")
    assert out.clip is not None and reused_content_warnings(out.clip)
    with_intro = rig.review.update(rig.cands[0].id, commentary_text="My take on this")
    assert with_intro.clip is not None and reused_content_warnings(with_intro.clip) == []
    desc = export_description("Insane clutch", "wait for it", "Clip: twitch.tv/streamer")
    assert desc.endswith("Clip: twitch.tv/streamer") and desc.startswith("Insane clutch")
