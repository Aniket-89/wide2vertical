"""M4 use case: candidate → clip → vertical captioned MP4 (preview or final).

Idempotent by content: the render hash covers everything that affects the pixels (source,
times, layout/crop, caption words and style, credit, intro, renderer version, quality). The
same inputs return the stored render without re-encoding.
"""

from __future__ import annotations

import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import structlog

from clipforge.domain.values import BlobKey, Stage
from clipforge.ports.infra import BlobStore, EventBus
from clipforge.ports.pipeline import PipelineError, ResolvedVideo, VideoRecord
from clipforge.ports.render import (
    ClipRecord,
    FacecamDetector,
    Renderer,
    RenderFailed,
    RenderPipelineStore,
    RenderRecord,
    RenderSpec,
    SectionFetcher,
    SectionFile,
)
from clipforge.rendering.captions import CaptionStyle, build_ass, clip_words
from clipforge.rendering.layout import (
    LAYOUT_RULES_VERSION,
    FrameFaces,
    LayoutPlan,
    choose_layout,
)
from clipforge.services._common import require_not_retired, sha256_text

log = structlog.get_logger(__name__)

KINDS = ("preview", "final")


@dataclass(frozen=True)
class RenderConfig:
    preview_size: tuple[int, int] = (540, 960)
    final_size: tuple[int, int] = (1080, 1920)
    preview_max_height: int = 720
    final_max_height: int = 1080
    pad_s: float = 1.0  # fetched around the clip so trims within ±pad need no re-download
    face_samples: int = 8


@dataclass(frozen=True)
class RenderOutcome:
    render: RenderRecord
    cached: bool
    layout: str
    layout_reason: str


def export_description(title: str, hook: str, credit: str) -> str:
    """Post description: title, hook, and (always, when set) the source's required credit."""
    parts = [p.strip() for p in (title, hook) if p and p.strip()]
    if credit.strip():
        parts.append(credit.strip())
    return "\n\n".join(parts)


class RenderPipeline:
    def __init__(
        self,
        store: RenderPipelineStore,
        sections: SectionFetcher,
        detector: FacecamDetector,
        renderer: Renderer,
        blobs: BlobStore,
        events: EventBus,
        cfg: RenderConfig | None = None,
    ) -> None:
        self.store = store
        self.sections = sections
        self.detector = detector
        self.renderer = renderer
        self.blobs = blobs
        self.events = events
        self.cfg = cfg or RenderConfig()

    # ------------------------------------------------------------ clips
    def ensure_clip(self, candidate_id: UUID) -> ClipRecord:
        cand = self.store.get_candidate(candidate_id)
        if cand is None:
            raise PipelineError("candidate not found")
        existing = self.store.get_clip_for_candidate(candidate_id)
        if existing is not None:
            return existing
        video = self._video(cand.video_id)
        require_not_retired(self.store, video, Stage.RENDER)  # don't recreate hidden clips
        src = self.store.source_for_video(video)
        credit = src.required_credit_text if src else ""
        return self.store.create_clip(cand, credit)

    def _video(self, video_id: UUID) -> VideoRecord:
        video = self.store.get_video(video_id)
        if video is None:
            raise PipelineError("video not found")
        return video

    # ------------------------------------------------------------ sections (cached in blobs)
    def _section(
        self, video: VideoRecord, start_s: float, end_s: float, max_height: int, workdir: Path
    ) -> SectionFile:
        a = max(0.0, start_s - self.cfg.pad_s)
        b = end_s + self.cfg.pad_s
        if video.duration_s:
            b = min(b, video.duration_s)
        name = f"{round(a * 1000)}-{round(b * 1000)}-{max_height}"
        ws = self.store.workspace_id
        vkey = BlobKey.for_workspace(ws, "videos", str(video.id), "sections", f"{name}.mp4")
        mkey = BlobKey.for_workspace(ws, "videos", str(video.id), "sections", f"{name}.json")
        if self.blobs.exists(vkey) and self.blobs.exists(mkey):
            with self.blobs.open_local(mkey) as mp:
                meta = json.loads(mp.read_text(encoding="utf-8"))
            with self.blobs.open_local(vkey) as vp:
                local = workdir / "section_cached.mp4"
                local.write_bytes(vp.read_bytes())
            return SectionFile(local, float(meta["start_s"]), float(meta["duration_s"]))
        rv = ResolvedVideo(
            video.platform,
            video.url,
            video.external_id,
            video.channel_handle or "",
            video.channel_id,
            "",
            video.duration_s,
        )
        sec = self.sections.fetch_section(rv, a, b, workdir, max_height)
        self.blobs.put(vkey, sec.path)
        meta_path = workdir / "section_meta.json"
        meta_path.write_text(json.dumps({"start_s": sec.start_s, "duration_s": sec.duration_s}))
        self.blobs.put(mkey, meta_path)
        return sec

    # ------------------------------------------------------------ render
    def _inputs(self, clip: ClipRecord, kind: str) -> dict[str, Any]:
        video = self._video(clip.video_id)
        t = self.store.get_transcript(video.id)
        words = t[1] if t else []
        override_text = (clip.caption_override or {}).get("text")
        caption_words = clip_words(words, clip.start_s, clip.end_s, override_text)
        style = self.store.get_caption_style(clip.caption_style_id)
        src = self.store.source_for_video(video)
        crop = clip.crop_override or self.store.source_crop_override(src.id if src else None)
        return {
            "video": video,
            "words": caption_words,
            "style": CaptionStyle.from_spec(style.spec),
            "style_spec": dict(style.spec),
            "crop": crop,
            "kind": kind,
        }

    def render_hash(self, clip: ClipRecord, inputs: dict[str, Any]) -> str:
        video: VideoRecord = inputs["video"]
        payload = {
            "renderer": self.renderer.version,
            "layout_rules": LAYOUT_RULES_VERSION,
            "kind": inputs["kind"],
            "video": f"{video.platform.value}:{video.external_id}",
            "t": [round(clip.start_s, 3), round(clip.end_s, 3)],
            "layout": clip.layout,
            "crop": inputs["crop"],
            "words": [(w.text, round(w.start, 3), round(w.end, 3)) for w in inputs["words"]],
            "style": inputs["style_spec"],
            "credit": clip.credit_text,
            "intro": clip.commentary_text,
        }
        return sha256_text(json.dumps(payload, sort_keys=True, default=str))

    def render(self, clip_id: UUID, kind: str, *, force: bool = False) -> RenderOutcome:
        if kind not in KINDS:
            raise PipelineError(f"unknown render kind {kind!r}")
        clip = self.store.get_clip(clip_id)
        if clip is None:
            raise PipelineError("clip not found")
        inputs = self._inputs(clip, kind)
        video: VideoRecord = inputs["video"]
        require_not_retired(self.store, video, Stage.RENDER)
        h = self.render_hash(clip, inputs)

        existing = self.store.find_render(clip.id, kind, h)
        if (
            not force
            and existing
            and existing.status == "succeeded"
            and existing.blob_key
            and self.blobs.exists(BlobKey(existing.blob_key))
        ):
            # mark it the clip's current render again (downloads serve the newest)
            rec = self.store.save_render(
                clip.id,
                kind,
                h,
                status="succeeded",
                blob_key=existing.blob_key,
                duration_s=existing.duration_s,
                render_seconds=existing.render_seconds,
            )
            return RenderOutcome(rec, True, clip.layout, "cached")

        self.store.save_render(clip.id, kind, h, status="running")
        try:
            outcome = self._render_now(clip, inputs, kind, h)
        except PipelineError as exc:
            self.store.save_render(clip.id, kind, h, status="failed")
            self.store.audit("render.failed", "clip", clip.id, kind=kind, error=str(exc)[:500])
            raise
        return outcome

    def _render_now(
        self, clip: ClipRecord, inputs: dict[str, Any], kind: str, h: str
    ) -> RenderOutcome:
        video: VideoRecord = inputs["video"]
        out_w, out_h = self.cfg.preview_size if kind == "preview" else self.cfg.final_size
        max_h = self.cfg.preview_max_height if kind == "preview" else self.cfg.final_max_height
        dur = clip.end_s - clip.start_s
        with tempfile.TemporaryDirectory(prefix="clipforge-render-") as tmp:
            workdir = Path(tmp)
            sec = self._section(video, clip.start_s, clip.end_s, max_h, workdir)
            offset = clip.start_s - sec.start_s
            if offset < -0.05 or offset + dur > sec.duration_s + 0.5:
                raise RenderFailed(
                    f"section {sec.start_s:.1f}+{sec.duration_s:.1f}s does not cover the clip"
                )
            plan = self._layout(clip, inputs, sec, offset, dur)
            ass = build_ass(
                inputs["words"],
                inputs["style"],
                dur,
                width=out_w,
                height=out_h,
                credit=clip.credit_text,
                intro=clip.commentary_text,
            )
            out = workdir / f"{kind}.mp4"
            result = self.renderer.render(
                RenderSpec(sec.path, max(0.0, offset), dur, plan, ass, out_w, out_h, kind, out)
            )
            key = BlobKey.for_workspace(
                self.store.workspace_id, "clips", str(clip.id), f"{kind}-{h[:12]}.mp4"
            )
            size = self.blobs.put(key, out)
        rec = self.store.save_render(
            clip.id,
            kind,
            h,
            status="succeeded",
            blob_key=key.value,
            duration_s=result.duration_s,
            render_seconds=result.render_seconds,
        )
        self.store.record_usage(
            "render_seconds",
            result.render_seconds,
            "seconds",
            video.id,
            render_kind=kind,
            clip_id=str(clip.id),
        )
        self.store.record_usage("storage_bytes", size, "bytes", video.id, asset=f"render_{kind}")
        log.info(
            "rendered",
            clip_id=str(clip.id),
            kind=kind,
            layout=plan.layout,
            seconds=result.render_seconds,
        )
        return RenderOutcome(rec, False, plan.layout, plan.reason)

    def _layout(
        self,
        clip: ClipRecord,
        inputs: dict[str, Any],
        sec: SectionFile,
        offset: float,
        dur: float,
    ) -> LayoutPlan:
        crop = inputs["crop"]
        needs_faces = not crop and clip.layout in (
            "auto",
            "center_crop",
            "facecam_top_gameplay_bottom",
        )
        if needs_faces:
            n = self.cfg.face_samples
            times = [offset + (i + 0.5) * dur / n for i in range(n)]
            faces = self.detector.analyze(sec.path, times)
        else:
            probe = self.detector.analyze(sec.path, [offset + dur / 2])  # frame size only
            faces = FrameFaces(probe.width, probe.height, [])
        return choose_layout(faces, requested=clip.layout, override=crop)

    def preview_top(self, video_id: UUID, n: int) -> list[RenderOutcome]:
        """Previews for the n best pending candidates; one failure doesn't stop the rest."""
        out: list[RenderOutcome] = []
        for c in self.store.list_candidates(video_id, limit=n):
            if c.status == "rejected":
                continue
            try:
                clip = self.ensure_clip(c.id)
                out.append(self.render(clip.id, "preview"))
            except PipelineError as exc:
                log.warning("preview_failed", candidate_id=str(c.id), error=str(exc)[:300])
        return out
