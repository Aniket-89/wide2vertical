"""SQLAlchemy ORM models — the full MVP schema (spec §4.4).

Conventions
- UUIDv7 primary keys, generated in the app.
- Every business table carries ``workspace_id`` (via ``WorkspaceScoped``).
- All timestamps are TIMESTAMPTZ (UTC). Soft delete via ``deleted_at`` where it matters.
- Enumerations are TEXT + CHECK constraints (easier to evolve than PG enums).
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    ARRAY,
    REAL,
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    MetaData,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, declared_attr, mapped_column

from clipforge.domain.ids import new_id
from clipforge.domain.values import ClipPolicy, JobStatus, Role, SourceStatus

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


def _in(col: str, values: list[str]) -> str:
    quoted = ", ".join(f"'{v}'" for v in values)
    return f"{col} IN ({quoted})"


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


class IdTimestamps:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=new_id)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class WorkspaceScoped(IdTimestamps):
    @declared_attr
    def workspace_id(cls) -> Mapped[UUID]:
        return mapped_column(
            ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
        )


class SoftDelete:
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


TS = DateTime(timezone=True)


# ------------------------------------------------------------------ tenancy
class Workspace(IdTimestamps, Base):
    __tablename__ = "workspaces"
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(100), unique=True)


class User(IdTimestamps, Base):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(String(320), unique=True)
    display_name: Mapped[str] = mapped_column(String(200), default="")


class Membership(WorkspaceScoped, Base):
    __tablename__ = "memberships"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(20), default=Role.OWNER.value)
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id"),
        CheckConstraint(_in("role", [r.value for r in Role]), name="role"),
    )


# ------------------------------------------------------------------ compliance
class Source(WorkspaceScoped, SoftDelete, Base):
    __tablename__ = "sources"
    platform: Mapped[str] = mapped_column(String(20))
    channel_handle: Mapped[str] = mapped_column(String(200))
    channel_id: Mapped[str | None] = mapped_column(String(200))
    clip_policy: Mapped[str] = mapped_column(String(30), default=ClipPolicy.UNKNOWN.value)
    policy_url: Mapped[str | None] = mapped_column(Text)
    required_credit_text: Mapped[str] = mapped_column(Text, default="")
    notes: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(20), default=SourceStatus.ACTIVE.value)
    retired_at: Mapped[datetime | None] = mapped_column(TS)
    crop_override: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    __table_args__ = (
        UniqueConstraint("workspace_id", "platform", "channel_handle"),
        CheckConstraint(_in("clip_policy", [p.value for p in ClipPolicy]), name="clip_policy"),
        CheckConstraint(_in("status", [s.value for s in SourceStatus]), name="status"),
    )


class AuditLog(WorkspaceScoped, Base):
    __tablename__ = "audit_log"
    actor_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id"))
    action: Mapped[str] = mapped_column(String(100))
    target_type: Mapped[str] = mapped_column(String(50))
    target_id: Mapped[UUID | None]
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


# ---------------------------------------------- config (versioned, immutable rows)
class ScoringProfile(WorkspaceScoped, Base):
    __tablename__ = "scoring_profiles"
    name: Mapped[str] = mapped_column(String(100))
    version: Mapped[int] = mapped_column(Integer, default=1)
    weights: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    params: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    __table_args__ = (UniqueConstraint("workspace_id", "name", "version"),)


class CaptionStyle(WorkspaceScoped, Base):
    __tablename__ = "caption_styles"
    name: Mapped[str] = mapped_column(String(100))
    version: Mapped[int] = mapped_column(Integer, default=1)
    spec: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    __table_args__ = (UniqueConstraint("workspace_id", "name", "version"),)


# ------------------------------------------------------------------ media
class Video(WorkspaceScoped, SoftDelete, Base):
    __tablename__ = "videos"
    source_id: Mapped[UUID | None] = mapped_column(ForeignKey("sources.id"))
    url: Mapped[str] = mapped_column(Text)
    platform: Mapped[str] = mapped_column(String(20))
    external_id: Mapped[str] = mapped_column(String(200))
    channel_handle: Mapped[str | None] = mapped_column(String(200), index=True)
    channel_id: Mapped[str | None] = mapped_column(String(200))
    title: Mapped[str] = mapped_column(Text, default="")
    duration_s: Mapped[float | None] = mapped_column(Float)
    recorded_at: Mapped[datetime | None] = mapped_column(TS)
    override_used: Mapped[bool] = mapped_column(Boolean, default=False)
    scoring_profile_id: Mapped[UUID | None] = mapped_column(ForeignKey("scoring_profiles.id"))
    status: Mapped[str] = mapped_column(String(30), default="pending")
    __table_args__ = (UniqueConstraint("workspace_id", "platform", "external_id"),)


class VideoAsset(WorkspaceScoped, Base):
    __tablename__ = "video_assets"
    video_id: Mapped[UUID] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(30))  # audio | proxy | full | chat_dump | window
    blob_key: Mapped[str] = mapped_column(Text)
    content_hash: Mapped[str] = mapped_column(String(64))
    bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    duration_s: Mapped[float | None] = mapped_column(Float)
    expires_at: Mapped[datetime | None] = mapped_column(TS)
    __table_args__ = (UniqueConstraint("video_id", "kind", "content_hash"),)


class Transcript(WorkspaceScoped, Base):
    __tablename__ = "transcripts"
    video_id: Mapped[UUID] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    model: Mapped[str] = mapped_column(String(100))
    language: Mapped[str | None] = mapped_column(String(10))
    content_hash: Mapped[str] = mapped_column(String(64))
    config_version: Mapped[str] = mapped_column(String(64))
    words: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    __table_args__ = (UniqueConstraint("video_id", "content_hash", "config_version"),)


class SignalSeries(WorkspaceScoped, Base):
    __tablename__ = "signal_series"
    video_id: Mapped[UUID] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    signal_name: Mapped[str] = mapped_column(String(50))
    signal_version: Mapped[str] = mapped_column(String(20))
    resolution_s: Mapped[float] = mapped_column(Float, default=1.0)
    values: Mapped[list[float]] = mapped_column(ARRAY(REAL))
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    input_hash: Mapped[str] = mapped_column(String(64))
    __table_args__ = (UniqueConstraint("video_id", "signal_name", "input_hash"),)


# ------------------------------------------------------------------ candidates & clips
class Candidate(WorkspaceScoped, Base):
    __tablename__ = "candidates"
    video_id: Mapped[UUID] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"), index=True)
    scoring_profile_id: Mapped[UUID] = mapped_column(ForeignKey("scoring_profiles.id"))
    start_s: Mapped[float] = mapped_column(Float)
    end_s: Mapped[float] = mapped_column(Float)
    score: Mapped[float] = mapped_column(Float)
    components: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    judge: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    title_suggestions: Mapped[list[str]] = mapped_column(ARRAY(Text), default=list)
    hook_text: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    rank: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    __table_args__ = (
        UniqueConstraint("video_id", "scoring_profile_id", "start_s", "end_s"),
        CheckConstraint(_in("status", ["pending", "approved", "rejected"]), name="status"),
    )


class Clip(WorkspaceScoped, SoftDelete, Base):
    __tablename__ = "clips"
    candidate_id: Mapped[UUID] = mapped_column(ForeignKey("candidates.id"), unique=True)
    start_s: Mapped[float] = mapped_column(Float)
    end_s: Mapped[float] = mapped_column(Float)
    caption_override: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    layout: Mapped[str] = mapped_column(String(50), default="center_crop")
    caption_style_id: Mapped[UUID | None] = mapped_column(ForeignKey("caption_styles.id"))
    crop_override: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    commentary_text: Mapped[str] = mapped_column(Text, default="")
    credit_text: Mapped[str] = mapped_column(Text, default="")


class ClipRender(WorkspaceScoped, Base):
    __tablename__ = "clip_renders"
    clip_id: Mapped[UUID] = mapped_column(ForeignKey("clips.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(10))
    render_hash: Mapped[str] = mapped_column(String(64))
    blob_key: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default=JobStatus.PENDING.value)
    duration_s: Mapped[float | None] = mapped_column(Float)
    render_seconds: Mapped[float | None] = mapped_column(Float)
    __table_args__ = (
        UniqueConstraint("clip_id", "kind", "render_hash"),
        CheckConstraint(_in("kind", ["preview", "final"]), name="kind"),
    )


# ------------------------------------------------------------------ publishing
class PublishTarget(WorkspaceScoped, Base):
    __tablename__ = "publish_targets"
    platform: Mapped[str] = mapped_column(String(30))
    account_label: Mapped[str] = mapped_column(String(200))
    secret_ref: Mapped[str | None] = mapped_column(Text)  # pointer into SecretBox, never a token
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class Publication(WorkspaceScoped, Base):
    __tablename__ = "publications"
    clip_id: Mapped[UUID] = mapped_column(ForeignKey("clips.id"), index=True)
    publish_target_id: Mapped[UUID] = mapped_column(ForeignKey("publish_targets.id"))
    status: Mapped[str] = mapped_column(String(20), default="scheduled")
    external_id: Mapped[str | None] = mapped_column(String(200))
    scheduled_at: Mapped[datetime | None] = mapped_column(TS)
    published_at: Mapped[datetime | None] = mapped_column(TS)
    idempotency_key: Mapped[str] = mapped_column(String(200), unique=True)


# ------------------------------------------------------------------ jobs
class Job(WorkspaceScoped, Base):
    __tablename__ = "jobs"
    video_id: Mapped[UUID | None] = mapped_column(ForeignKey("videos.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20), default=JobStatus.PENDING.value)
    input_hash: Mapped[str] = mapped_column(String(64))
    priority: Mapped[int] = mapped_column(Integer, default=5)
    __table_args__ = (CheckConstraint(_in("status", [s.value for s in JobStatus]), name="status"),)


class JobRun(WorkspaceScoped, Base):
    __tablename__ = "job_runs"
    job_id: Mapped[UUID] = mapped_column(ForeignKey("jobs.id", ondelete="CASCADE"), index=True)
    stage: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(20), default=JobStatus.RUNNING.value)
    attempt: Mapped[int] = mapped_column(Integer, default=1)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(TS)


# ------------------------------------------------------------------ data for tuning & billing
class FeedbackEvent(WorkspaceScoped, Base):
    __tablename__ = "feedback_events"
    candidate_id: Mapped[UUID | None] = mapped_column(ForeignKey("candidates.id"))
    clip_id: Mapped[UUID | None] = mapped_column(ForeignKey("clips.id"))
    kind: Mapped[str] = mapped_column(String(50))
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())


class UsageEvent(WorkspaceScoped, Base):
    """The metering contract a future billing integration will read."""

    __tablename__ = "usage_events"
    kind: Mapped[str] = mapped_column(String(50), index=True)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 6))
    unit: Mapped[str] = mapped_column(String(20))
    video_id: Mapped[UUID | None] = mapped_column(ForeignKey("videos.id", ondelete="SET NULL"))
    meta: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    occurred_at: Mapped[datetime] = mapped_column(TS, server_default=func.now(), index=True)


class IdempotencyKey(Base):
    __tablename__ = "idempotency_keys"
    workspace_id: Mapped[UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), primary_key=True
    )
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    request_hash: Mapped[str] = mapped_column(String(64))
    response: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(TS, server_default=func.now())
