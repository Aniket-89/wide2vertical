"""SQLAlchemy implementation of ``PipelineStore``. Workspace-scoped via the repositories."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

import numpy as np
from sqlalchemy import delete, or_, select, update
from sqlalchemy.orm import Session

from clipforge.domain.values import (
    BlobKey,
    ClipPolicy,
    JobStatus,
    Platform,
    SourceStatus,
    Stage,
    WorkspaceId,
)
from clipforge.infra.db import models as m
from clipforge.infra.db.repositories import AuditRepo, JobRepo, UsageRepo, VideoRepo
from clipforge.ports.analysis import (
    CandidateWindow,
    ScoringProfileRecord,
    SignalSeries,
    StoredCandidate,
)
from clipforge.ports.pipeline import (
    AssetRecord,
    PipelineError,
    ResolvedVideo,
    SourceRecord,
    Transcript,
    VideoRecord,
    Word,
)


def _source_record(s: m.Source) -> SourceRecord:
    return SourceRecord(
        id=s.id,
        platform=Platform(s.platform),
        channel_handle=s.channel_handle,
        clip_policy=ClipPolicy(s.clip_policy),
        status=SourceStatus(s.status),
        required_credit_text=s.required_credit_text,
    )


def _video_record(v: m.Video) -> VideoRecord:
    return VideoRecord(
        id=v.id,
        workspace_id=WorkspaceId(v.workspace_id),
        source_id=v.source_id,
        platform=Platform(v.platform),
        url=v.url,
        external_id=v.external_id,
        status=v.status,
        duration_s=v.duration_s,
        channel_handle=v.channel_handle,
        channel_id=v.channel_id,
    )


class SqlPipelineStore:
    def __init__(
        self, session: Session, workspace_id: WorkspaceId, actor_user_id: UUID | None = None
    ) -> None:
        self.session = session
        self.workspace_id = workspace_id
        self.actor_user_id = actor_user_id
        self._videos = VideoRepo(session, workspace_id)
        self._jobs = JobRepo(session, workspace_id)
        self._audit = AuditRepo(session, workspace_id)
        self._usage = UsageRepo(session, workspace_id)

    # -- sources
    def find_source(
        self, platform: Platform, channel_handle: str, channel_id: str | None
    ) -> SourceRecord | None:
        conds = [m.Source.channel_handle == channel_handle]
        if channel_id:
            conds.append(m.Source.channel_id == channel_id)
        stmt = select(m.Source).where(
            m.Source.workspace_id == self.workspace_id,
            m.Source.deleted_at.is_(None),
            m.Source.platform == platform.value,
            or_(*conds),
        )
        s = self.session.scalars(stmt).first()
        return _source_record(s) if s else None

    # -- videos
    def _video_by_external(self, platform: Platform, external_id: str) -> m.Video | None:
        stmt = select(m.Video).where(
            m.Video.workspace_id == self.workspace_id,
            m.Video.platform == platform.value,
            m.Video.external_id == external_id,
        )
        return self.session.scalars(stmt).one_or_none()

    def video_by_external(self, platform: Platform, external_id: str) -> VideoRecord | None:
        v = self._video_by_external(platform, external_id)
        return _video_record(v) if v else None

    def upsert_video(
        self, rv: ResolvedVideo, source_id: UUID | None, override_used: bool
    ) -> tuple[VideoRecord, bool]:
        v = self._video_by_external(rv.platform, rv.external_id)
        created = v is None
        if v is None:
            v = m.Video(
                workspace_id=self.workspace_id,
                platform=rv.platform.value,
                external_id=rv.external_id,
                status="ingested",
            )
        v.url, v.title, v.duration_s = rv.url, rv.title, rv.duration_s
        v.channel_handle, v.channel_id = rv.channel_handle, rv.channel_id
        v.source_id = source_id
        v.override_used = v.override_used or override_used
        v.deleted_at = None
        if created:
            self._videos.add(v)
        self.session.flush()
        return _video_record(v), created

    def get_video(self, video_id: UUID) -> VideoRecord | None:
        v = self._videos.get(video_id)
        return _video_record(v) if v else None

    def source_for_video(self, video: VideoRecord) -> SourceRecord | None:
        if video.source_id is not None:
            s = self.session.get(m.Source, video.source_id)
            if s is not None and s.workspace_id == self.workspace_id:
                return _source_record(s)
        if video.channel_handle:
            return self.find_source(video.platform, video.channel_handle, video.channel_id)
        return None

    def set_video_status(self, video_id: UUID, status: str) -> None:
        self.session.execute(
            update(m.Video)
            .where(m.Video.workspace_id == self.workspace_id, m.Video.id == video_id)
            .values(status=status)
        )

    # -- assets
    def find_asset(self, video_id: UUID, kind: str) -> AssetRecord | None:
        stmt = (
            select(m.VideoAsset)
            .where(
                m.VideoAsset.workspace_id == self.workspace_id,
                m.VideoAsset.video_id == video_id,
                m.VideoAsset.kind == kind,
            )
            .order_by(m.VideoAsset.id.desc())
        )
        a = self.session.scalars(stmt).first()
        if a is None:
            return None
        return AssetRecord(BlobKey(a.blob_key), a.content_hash, a.bytes, a.duration_s)

    def save_asset(self, video_id: UUID, kind: str, asset: AssetRecord) -> None:
        stmt = select(m.VideoAsset).where(
            m.VideoAsset.workspace_id == self.workspace_id,
            m.VideoAsset.video_id == video_id,
            m.VideoAsset.kind == kind,
            m.VideoAsset.content_hash == asset.content_hash,
        )
        row = self.session.scalars(stmt).one_or_none()
        if row is None:
            row = m.VideoAsset(
                workspace_id=self.workspace_id,
                video_id=video_id,
                kind=kind,
                content_hash=asset.content_hash,
            )
            self.session.add(row)
        row.blob_key, row.bytes, row.duration_s = (
            asset.blob_key.value,
            asset.bytes,
            asset.duration_s,
        )
        self.session.flush()

    # -- transcripts
    def has_transcript(self, video_id: UUID, content_hash: str, config_version: str) -> bool:
        stmt = select(m.Transcript.id).where(
            m.Transcript.workspace_id == self.workspace_id,
            m.Transcript.video_id == video_id,
            m.Transcript.content_hash == content_hash,
            m.Transcript.config_version == config_version,
        )
        return self.session.scalars(stmt).first() is not None

    def save_transcript(
        self, video_id: UUID, content_hash: str, config_version: str, t: Transcript
    ) -> None:
        if self.has_transcript(video_id, content_hash, config_version):
            return
        self.session.add(
            m.Transcript(
                workspace_id=self.workspace_id,
                video_id=video_id,
                model=t.model,
                language=t.language,
                content_hash=content_hash,
                config_version=config_version,
                words=[{"w": w.text, "s": w.start, "e": w.end, "p": w.prob} for w in t.words],
            )
        )
        self.session.flush()

    # -- stage tracking: one jobs row per (video, stage); one job_runs row per attempt
    def begin_stage(self, video_id: UUID, stage: Stage, input_hash: str) -> UUID:
        stmt = select(m.Job).where(
            m.Job.workspace_id == self.workspace_id,
            m.Job.video_id == video_id,
            m.Job.kind == stage.value,
        )
        job = self.session.scalars(stmt).one_or_none()
        if job is None:
            job = self._jobs.add(
                m.Job(
                    workspace_id=self.workspace_id,
                    video_id=video_id,
                    kind=stage.value,
                    input_hash=input_hash,
                )
            )
        job.status, job.input_hash = JobStatus.RUNNING.value, input_hash
        attempts = len(
            self.session.scalars(select(m.JobRun.id).where(m.JobRun.job_id == job.id)).all()
        )
        run = m.JobRun(
            workspace_id=self.workspace_id,
            job_id=job.id,
            stage=stage.value,
            status=JobStatus.RUNNING.value,
            attempt=attempts + 1,
        )
        self.session.add(run)
        self.session.flush()
        return run.id

    def end_stage(self, run_id: UUID, status: JobStatus, error: str | None = None) -> None:
        run = self.session.get(m.JobRun, run_id)
        if run is None or run.workspace_id != self.workspace_id:
            return
        run.status, run.error, run.finished_at = status.value, error, datetime.now(UTC)
        job = self.session.get(m.Job, run.job_id)
        if job is not None:
            job.status = status.value
        self.session.flush()

    # -- metering & audit
    def record_usage(
        self, kind: str, quantity: float, unit: str, video_id: UUID | None, **meta: object
    ) -> None:
        self._usage.add(
            m.UsageEvent(
                workspace_id=self.workspace_id,
                kind=kind,
                quantity=Decimal(str(quantity)),
                unit=unit,
                video_id=video_id,
                meta=dict(meta),
            )
        )

    def audit(
        self, action: str, target_type: str, target_id: UUID | None, **details: object
    ) -> None:
        self._audit.record(action, target_type, target_id, self.actor_user_id, **details)

    # -- analysis (M2)
    def get_transcript(self, video_id: UUID) -> tuple[str, list[Word]] | None:
        t = self.session.scalars(
            select(m.Transcript)
            .where(
                m.Transcript.workspace_id == self.workspace_id, m.Transcript.video_id == video_id
            )
            .order_by(m.Transcript.id.desc())
        ).first()
        if t is None:
            return None
        words = [
            Word(w["w"], float(w["s"]), float(w["e"]), float(w.get("p", 1.0))) for w in t.words
        ]
        return f"{t.content_hash}:{t.config_version}", words

    def get_scoring_profile(self, name: str | None) -> ScoringProfileRecord:
        stmt = select(m.ScoringProfile).where(m.ScoringProfile.workspace_id == self.workspace_id)
        if name is None:
            stmt = stmt.order_by(
                m.ScoringProfile.is_default.desc(), m.ScoringProfile.version.desc()
            )
        else:
            stmt = stmt.where(m.ScoringProfile.name == name).order_by(
                m.ScoringProfile.version.desc()
            )
        p = self.session.scalars(stmt).first()
        if p is None:
            raise PipelineError(f"scoring profile {name or '(default)'} not found")
        return ScoringProfileRecord(p.id, p.name, p.version, dict(p.weights), dict(p.params))

    def find_signal(self, video_id: UUID, name: str, input_hash: str) -> SignalSeries | None:
        row = self.session.scalars(
            select(m.SignalSeries).where(
                m.SignalSeries.workspace_id == self.workspace_id,
                m.SignalSeries.video_id == video_id,
                m.SignalSeries.signal_name == name,
                m.SignalSeries.input_hash == input_hash,
            )
        ).first()
        if row is None:
            return None
        return SignalSeries(
            row.signal_name,
            row.signal_version,
            np.asarray(row.values, dtype=np.float64),
            dict(row.meta),
        )

    def save_signal(self, video_id: UUID, s: SignalSeries, input_hash: str) -> None:
        if self.find_signal(video_id, s.name, input_hash) is not None:
            return
        self.session.add(
            m.SignalSeries(
                workspace_id=self.workspace_id,
                video_id=video_id,
                signal_name=s.name,
                signal_version=s.version,
                resolution_s=1.0,
                values=[round(float(v), 4) for v in s.values],
                meta=dict(s.meta),
                input_hash=input_hash,
            )
        )
        self.session.flush()

    def replace_candidates(
        self, video_id: UUID, profile_id: UUID, windows: Sequence[CandidateWindow]
    ) -> int:
        scope = (
            m.Candidate.workspace_id == self.workspace_id,
            m.Candidate.video_id == video_id,
            m.Candidate.scoring_profile_id == profile_id,
        )
        has_clip = select(m.Clip.candidate_id).where(m.Clip.workspace_id == self.workspace_id)
        self.session.execute(
            delete(m.Candidate).where(
                *scope, m.Candidate.status == "pending", m.Candidate.id.not_in(has_clip)
            )
        )
        kept = {
            (c.start_s, c.end_s) for c in self.session.scalars(select(m.Candidate).where(*scope))
        }
        inserted = 0
        for rank, w in enumerate(windows, start=1):
            if (w.start_s, w.end_s) in kept:  # already reviewed: keep the human decision
                continue
            self.session.add(
                m.Candidate(
                    workspace_id=self.workspace_id,
                    video_id=video_id,
                    scoring_profile_id=profile_id,
                    start_s=w.start_s,
                    end_s=w.end_s,
                    score=w.score,
                    components={**w.components, "peak_s": w.peak_s},
                    reason=w.reason,
                    rank=rank,
                    status="pending",
                )
            )
            inserted += 1
        self.session.flush()
        return inserted

    def list_candidates(
        self, video_id: UUID, profile_id: UUID | None = None, limit: int = 100
    ) -> list[StoredCandidate]:
        stmt = select(m.Candidate).where(
            m.Candidate.workspace_id == self.workspace_id, m.Candidate.video_id == video_id
        )
        if profile_id is not None:
            stmt = stmt.where(m.Candidate.scoring_profile_id == profile_id)
        stmt = stmt.order_by(m.Candidate.score.desc()).limit(limit)
        return [
            StoredCandidate(
                c.id,
                c.rank,
                c.start_s,
                c.end_s,
                c.score,
                dict(c.components),
                c.reason,
                c.status,
                tuple(c.title_suggestions or ()),
                c.hook_text,
                dict(c.judge) if c.judge else None,
            )
            for c in self.session.scalars(stmt)
        ]

    def apply_judgement(
        self,
        candidate_id: UUID,
        *,
        score: float,
        components: Mapping[str, Any],
        judge: Mapping[str, Any],
        titles: Sequence[str],
        hook: str,
        reason: str,
    ) -> None:
        c = self.session.get(m.Candidate, candidate_id)
        if c is None or c.workspace_id != self.workspace_id:
            return
        c.score, c.components, c.judge = score, dict(components), dict(judge)
        c.title_suggestions, c.hook_text, c.reason = list(titles), hook, reason
        self.session.flush()

    def rerank(self, video_id: UUID, profile_id: UUID) -> None:
        rows = self.session.scalars(
            select(m.Candidate)
            .where(
                m.Candidate.workspace_id == self.workspace_id,
                m.Candidate.video_id == video_id,
                m.Candidate.scoring_profile_id == profile_id,
            )
            .order_by(m.Candidate.score.desc(), m.Candidate.start_s)
        ).all()
        for i, c in enumerate(rows, start=1):
            c.rank = i
        self.session.flush()


def stage_overview(
    session: Session, workspace_id: WorkspaceId, video_id: UUID
) -> list[dict[str, Any]]:
    """Latest status per stage for one video (for CLI/API progress views)."""
    jobs = session.scalars(
        select(m.Job)
        .where(m.Job.workspace_id == workspace_id, m.Job.video_id == video_id)
        .order_by(m.Job.id)
    ).all()
    out = []
    for j in jobs:
        run = session.scalars(
            select(m.JobRun).where(m.JobRun.job_id == j.id).order_by(m.JobRun.id.desc())
        ).first()
        out.append(
            {
                "stage": j.kind,
                "status": j.status,
                "attempts": run.attempt if run else 0,
                "error": run.error if run else None,
                "started_at": run.started_at.isoformat() if run else None,
                "finished_at": run.finished_at.isoformat() if run and run.finished_at else None,
            }
        )
    return out
