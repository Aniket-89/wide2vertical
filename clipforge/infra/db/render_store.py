"""SQL implementation of RenderStore (clips, renders, feedback) on top of SqlPipelineStore."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import select

from clipforge.infra.db import models as m
from clipforge.infra.db.pipeline_store import SqlPipelineStore
from clipforge.ports.pipeline import PipelineError
from clipforge.ports.render import (
    CandidateRecord,
    CaptionStyleRecord,
    ClipRecord,
    RenderRecord,
)

CLIP_FIELDS = {
    "start_s", "end_s", "layout", "caption_style_id", "caption_override", "crop_override",
    "commentary_text", "credit_text",
}  # fmt: skip


class SqlRenderStore(SqlPipelineStore):
    # -- candidates
    def _candidate(self, candidate_id: UUID) -> m.Candidate | None:
        c = self.session.get(m.Candidate, candidate_id)
        return c if c is not None and c.workspace_id == self.workspace_id else None

    def get_candidate(self, candidate_id: UUID) -> CandidateRecord | None:
        c = self._candidate(candidate_id)
        if c is None:
            return None
        return CandidateRecord(
            c.id,
            c.video_id,
            c.scoring_profile_id,
            c.start_s,
            c.end_s,
            c.score,
            c.rank,
            dict(c.components),
            c.status,
            tuple(c.title_suggestions or ()),
            c.hook_text,
            c.reason,
            dict(c.judge) if c.judge else None,
        )

    def set_candidate_status(self, candidate_id: UUID, status: str) -> None:
        c = self._candidate(candidate_id)
        if c is not None:
            c.status = status
            self.session.flush()

    # -- clips
    def _clip_record(self, clip: m.Clip) -> ClipRecord:
        cand = self.session.get(m.Candidate, clip.candidate_id)
        assert cand is not None
        return ClipRecord(
            clip.id,
            clip.candidate_id,
            cand.video_id,
            clip.start_s,
            clip.end_s,
            clip.layout,
            clip.caption_style_id,
            dict(clip.caption_override) if clip.caption_override else None,
            dict(clip.crop_override) if clip.crop_override else None,
            clip.commentary_text,
            clip.credit_text,
        )

    def _clip(self, clip_id: UUID) -> m.Clip | None:
        c = self.session.get(m.Clip, clip_id)
        ok = c is not None and c.workspace_id == self.workspace_id and c.deleted_at is None
        return c if ok else None

    def get_clip(self, clip_id: UUID) -> ClipRecord | None:
        c = self._clip(clip_id)
        return self._clip_record(c) if c else None

    def get_clip_for_candidate(self, candidate_id: UUID) -> ClipRecord | None:
        c = self.session.scalars(
            select(m.Clip).where(
                m.Clip.workspace_id == self.workspace_id,
                m.Clip.candidate_id == candidate_id,
                m.Clip.deleted_at.is_(None),
            )
        ).first()
        return self._clip_record(c) if c else None

    def create_clip(self, candidate: CandidateRecord, credit_text: str) -> ClipRecord:
        existing = self.get_clip_for_candidate(candidate.id)
        if existing is not None:
            return existing
        clip = m.Clip(
            workspace_id=self.workspace_id,
            candidate_id=candidate.id,
            start_s=candidate.start_s,
            end_s=candidate.end_s,
            layout="auto",
            credit_text=credit_text,
        )
        self.session.add(clip)
        self.session.flush()
        return self._clip_record(clip)

    def update_clip(self, clip_id: UUID, **fields: Any) -> ClipRecord:
        bad = set(fields) - CLIP_FIELDS
        if bad:
            raise ValueError(f"unknown clip fields: {sorted(bad)}")
        c = self._clip(clip_id)
        if c is None:
            raise PipelineError("clip not found")
        for k, v in fields.items():
            setattr(c, k, v)
        self.session.flush()
        return self._clip_record(c)

    # -- styles / sources
    def get_caption_style(self, style_id: UUID | None) -> CaptionStyleRecord:
        stmt = select(m.CaptionStyle).where(m.CaptionStyle.workspace_id == self.workspace_id)
        if style_id is not None:
            stmt = stmt.where(m.CaptionStyle.id == style_id)
        else:
            stmt = stmt.order_by(m.CaptionStyle.created_at, m.CaptionStyle.version.desc())
        s = self.session.scalars(stmt).first()
        if s is None:
            raise PipelineError("caption style not found")
        return CaptionStyleRecord(s.id, s.name, dict(s.spec))

    def source_crop_override(self, source_id: UUID | None) -> dict[str, Any] | None:
        if source_id is None:
            return None
        s = self.session.get(m.Source, source_id)
        if s is None or s.workspace_id != self.workspace_id or not s.crop_override:
            return None
        return dict(s.crop_override)

    # -- renders
    def _render_record(self, r: m.ClipRender) -> RenderRecord:
        return RenderRecord(
            r.id,
            r.clip_id,
            r.kind,
            r.render_hash,
            r.blob_key,
            r.status,
            r.duration_s,
            r.render_seconds,
        )

    def find_render(self, clip_id: UUID, kind: str, render_hash: str) -> RenderRecord | None:
        r = self.session.scalars(
            select(m.ClipRender).where(
                m.ClipRender.workspace_id == self.workspace_id,
                m.ClipRender.clip_id == clip_id,
                m.ClipRender.kind == kind,
                m.ClipRender.render_hash == render_hash,
            )
        ).first()
        return self._render_record(r) if r else None

    def latest_render(self, clip_id: UUID, kind: str) -> RenderRecord | None:
        r = self.session.scalars(
            select(m.ClipRender)
            .where(
                m.ClipRender.workspace_id == self.workspace_id,
                m.ClipRender.clip_id == clip_id,
                m.ClipRender.kind == kind,
                m.ClipRender.status == "succeeded",
            )
            .order_by(m.ClipRender.id.desc())
        ).first()
        return self._render_record(r) if r else None

    def save_render(
        self,
        clip_id: UUID,
        kind: str,
        render_hash: str,
        *,
        status: str,
        blob_key: str | None = None,
        duration_s: float | None = None,
        render_seconds: float | None = None,
    ) -> RenderRecord:
        r = self.session.scalars(
            select(m.ClipRender).where(
                m.ClipRender.workspace_id == self.workspace_id,
                m.ClipRender.clip_id == clip_id,
                m.ClipRender.kind == kind,
                m.ClipRender.render_hash == render_hash,
            )
        ).first()
        if r is None:
            r = m.ClipRender(
                workspace_id=self.workspace_id, clip_id=clip_id, kind=kind, render_hash=render_hash
            )
            self.session.add(r)
        r.status, r.blob_key = status, blob_key
        r.duration_s, r.render_seconds = duration_s, render_seconds
        self.session.flush()
        return self._render_record(r)

    # -- feedback (training data for M8)
    def record_feedback(
        self, kind: str, candidate_id: UUID | None, clip_id: UUID | None, **payload: Any
    ) -> None:
        self.session.add(
            m.FeedbackEvent(
                workspace_id=self.workspace_id,
                candidate_id=candidate_id,
                clip_id=clip_id,
                kind=kind,
                payload=payload,
            )
        )
        self.session.flush()
