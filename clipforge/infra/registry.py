"""Composition root: the only place that imports concrete adapters.

Config picks adapters; everything else receives ports.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import UUID

import structlog
from pydantic import SecretStr
from sqlalchemy.orm import Session

from clipforge.adapters.audio.pyav_rms import PyAvRmsAnalyzer
from clipforge.adapters.blobstore.local import LocalBlobStore
from clipforge.adapters.chat.twitch_gql import TwitchGqlChatReplay
from clipforge.adapters.chat.youtube_live_chat import YouTubeLiveChatReplay
from clipforge.adapters.eventbus.in_process import InProcessEventBus
from clipforge.adapters.fetchers.ytdlp import YtDlpFetcher
from clipforge.adapters.judges.anthropic_judge import AnthropicJudge
from clipforge.adapters.judges.null_judge import NullJudge
from clipforge.adapters.judges.openai_compat_judge import OpenAICompatJudge
from clipforge.adapters.media.facecam_opencv import OpenCvFacecamDetector
from clipforge.adapters.media.sections import RoutingSectionFetcher
from clipforge.adapters.misc import AllowAllQuota, SystemClock
from clipforge.adapters.publishers.stubs import InstagramPublisher, TikTokPublisher
from clipforge.adapters.publishers.youtube import (
    YouTubePublisher,
    loopback_consent,
    read_client_secrets,
)
from clipforge.adapters.queue.dramatiq_queue import DISPATCH_ACTOR, DramatiqJobQueue, make_broker
from clipforge.adapters.render.ffmpeg_renderer import FfmpegRenderer
from clipforge.adapters.resolvers.ytdlp import KickResolver, TwitchResolver, YouTubeResolver
from clipforge.adapters.secrets.aesgcm import AesGcmSecretBox, generate_key
from clipforge.adapters.transcribers.faster_whisper import FasterWhisperTranscriber
from clipforge.domain.values import Platform, WorkspaceId
from clipforge.infra.config import Settings, get_settings
from clipforge.infra.db.pipeline_store import SqlPipelineStore
from clipforge.infra.db.publish_store import SqlPublishStore
from clipforge.infra.db.render_store import SqlRenderStore
from clipforge.ports.analysis import AudioAnalyzer, ChatProvider
from clipforge.ports.infra import BlobStore, Clock, EventBus, JobQueue, QuotaService
from clipforge.ports.judge import LLMJudge
from clipforge.ports.pipeline import MediaFetcher, PipelineError, SourceResolver, Transcriber
from clipforge.ports.publish import Publisher, SecretBox
from clipforge.ports.render import FacecamDetector, Renderer, SectionFetcher
from clipforge.services.analyze import AnalyzePipeline
from clipforge.services.ingest import IngestPipeline
from clipforge.services.judging import JudgeConfig
from clipforge.services.publish import PublishConfig, PublishService
from clipforge.services.render import RenderPipeline
from clipforge.services.review import ReviewService
from clipforge.signals import default_signals

log = structlog.get_logger(__name__)

# Job kinds → base queue. Transcription has its own queue family so GPU workers can own it.
QUEUE_FOR_KIND: dict[str, str] = {
    "pipeline.transcribe": "transcribe",
    "render.preview": "render",
    "render.final": "render",
}


@dataclass
class Container:
    settings: Settings
    clock: Clock
    blobs: BlobStore
    events: EventBus
    quota: QuotaService
    resolvers: list[SourceResolver]
    fetcher: MediaFetcher
    transcriber: Transcriber
    audio: AudioAnalyzer = field(default_factory=PyAvRmsAnalyzer)
    chat_overrides: dict[Platform, ChatProvider] | None = None  # tests inject fakes here
    judge_override: LLMJudge | None = None  # tests inject fakes here
    sections: SectionFetcher | None = None  # None = real adapters (lazy)
    detector: FacecamDetector | None = None
    renderer: Renderer | None = None
    publishers: dict[str, Publisher] | None = None  # None = real adapters (lazy)
    secrets: SecretBox | None = None  # None = built from CLIPFORGE_SECRET_KEY when set
    _queue: JobQueue | None = field(default=None, repr=False)

    @property
    def queue(self) -> JobQueue:
        if self._queue is None:  # lazy: CLI-only runs never need Redis
            self._queue = DramatiqJobQueue(make_broker(self.settings.redis_url), QUEUE_FOR_KIND)
        return self._queue

    def ingest_pipeline(
        self, session: Session, workspace_id: WorkspaceId, actor_user_id: UUID | None = None
    ) -> IngestPipeline:
        return IngestPipeline(
            store=SqlPipelineStore(session, workspace_id, actor_user_id),
            resolvers=self.resolvers,
            fetcher=self.fetcher,
            transcriber=self.transcriber,
            blobs=self.blobs,
            events=self.events,
            quota=self.quota,
        )

    def render_pipeline(
        self, session: Session, workspace_id: WorkspaceId, actor_user_id: UUID | None = None
    ) -> RenderPipeline:
        if self.sections is None:
            self.sections = RoutingSectionFetcher()
        if self.detector is None:
            self.detector = OpenCvFacecamDetector()
        if self.renderer is None:
            self.renderer = FfmpegRenderer(timeout_s=self.settings.render_timeout_s)
        return RenderPipeline(
            store=SqlRenderStore(session, workspace_id, actor_user_id),
            sections=self.sections,
            detector=self.detector,
            renderer=self.renderer,
            blobs=self.blobs,
            events=self.events,
        )

    def review_service(
        self, session: Session, workspace_id: WorkspaceId, actor_user_id: UUID | None = None
    ) -> ReviewService:
        return ReviewService(SqlRenderStore(session, workspace_id, actor_user_id))

    def publish_service(
        self, session: Session, workspace_id: WorkspaceId, actor_user_id: UUID | None = None
    ) -> PublishService:
        if self.publishers is None:
            self.publishers = build_publishers(self.settings)
        if self.secrets is None:
            self.secrets = build_secret_box(self.settings)
        return PublishService(
            store=SqlPublishStore(session, workspace_id, actor_user_id),
            publishers=self.publishers,
            secrets=self.secrets,
            blobs=self.blobs,
            clock=self.clock,
            cfg=PublishConfig(
                autopublish=self.settings.enable_autopublish,
                stats_window_days=self.settings.stats_window_days,
            ),
        )

    def analyze_pipeline(
        self, session: Session, workspace_id: WorkspaceId, actor_user_id: UUID | None = None
    ) -> AnalyzePipeline:
        chat = (
            self.chat_overrides
            if self.chat_overrides is not None
            else build_chat_providers(self.settings, salt=str(workspace_id))
        )
        return AnalyzePipeline(
            store=SqlPipelineStore(session, workspace_id, actor_user_id),
            signals=default_signals(),
            chat_providers=chat,
            audio=self.audio,
            blobs=self.blobs,
            events=self.events,
            judge=self.judge_override or build_judge(self.settings),
            judge_config=build_judge_config(self.settings),
        )


def build_chat_providers(settings: Settings, salt: str) -> dict[Platform, ChatProvider]:
    """Unofficial chat adapters are opt-in per platform (ADR-0006). Kick: not built yet."""
    providers: dict[Platform, ChatProvider] = {}
    if settings.enable_twitch_chat_gql:
        providers[Platform.TWITCH] = TwitchGqlChatReplay(salt)
    if settings.enable_youtube_chat_replay:
        providers[Platform.YOUTUBE] = YouTubeLiveChatReplay(salt)
    return providers


_JUDGE_CACHE: dict[tuple[str, ...], LLMJudge] = {}


DEFAULT_ANTHROPIC_MODEL = "claude-haiku-5-5"


def build_judge(settings: Settings) -> LLMJudge:
    """A paid judge only when explicitly enabled AND configured; otherwise NullJudge with the
    reason. Provider picks the adapter (ADR-0030)."""
    if not settings.enable_llm_judge:
        return NullJudge("ENABLE_LLM_JUDGE is off")
    provider = settings.llm_provider

    def secret(s: SecretStr | None) -> str:
        return s.get_secret_value() if s else ""

    if provider == "anthropic":
        key, key_var = (
            secret(settings.anthropic_api_key) or secret(settings.llm_api_key),
            ("CLIPFORGE_ANTHROPIC_API_KEY"),
        )
    else:
        key, key_var = secret(settings.llm_api_key), "CLIPFORGE_LLM_API_KEY"
    if not key:
        log.warning("llm_judge_enabled_without_key", provider=provider)
        return NullJudge(f"ENABLE_LLM_JUDGE is on but {key_var} is empty")
    if provider != "anthropic" and settings.llm_model == DEFAULT_ANTHROPIC_MODEL:
        log.warning("llm_judge_model_not_set", provider=provider)
        return NullJudge(f"set CLIPFORGE_LLM_MODEL to a {provider} model id")
    if provider == "openai_compat" and not settings.llm_base_url:
        return NullJudge("provider openai_compat needs CLIPFORGE_LLM_BASE_URL")

    cache_key = (provider, settings.llm_model, settings.llm_base_url or "", key[-6:])
    if cache_key not in _JUDGE_CACHE:  # reuse one HTTP client per process
        judge: LLMJudge
        if provider == "anthropic":
            judge = AnthropicJudge(
                model=settings.llm_model,
                api_key=key,
                timeout_s=settings.llm_timeout_s,
                max_retries=settings.llm_max_retries,
            )
        else:
            judge = OpenAICompatJudge(
                provider=provider,
                model=settings.llm_model,
                api_key=key,
                base_url=settings.llm_base_url,
                timeout_s=settings.llm_timeout_s,
                max_retries=settings.llm_max_retries,
            )
        _JUDGE_CACHE[cache_key] = judge
    return _JUDGE_CACHE[cache_key]


def build_judge_config(settings: Settings) -> JudgeConfig:
    return JudgeConfig(
        batch_size=settings.judge_batch_size,
        max_windows_per_video=settings.judge_max_windows_per_video,
        price_in_per_mtok=settings.llm_price_in_per_mtok,
        price_out_per_mtok=settings.llm_price_out_per_mtok,
        price_cache_read_per_mtok=settings.llm_price_cache_read_per_mtok,
        price_cache_write_per_mtok=settings.llm_price_cache_write_per_mtok,
    )


def build_publishers(settings: Settings) -> dict[str, Publisher]:
    pubs: dict[str, Publisher] = {"youtube": YouTubePublisher()}
    if settings.enable_instagram_publish:
        pubs["instagram"] = InstagramPublisher()
    if settings.enable_tiktok_publish:
        pubs["tiktok"] = TikTokPublisher()
    return pubs


def build_secret_box(settings: Settings) -> SecretBox | None:
    key = settings.secret_key.get_secret_value() if settings.secret_key else ""
    return AesGcmSecretBox(key) if key else None


def new_secret_key() -> str:
    return generate_key()


def connect_youtube(
    settings: Settings,
    workspace_id: WorkspaceId,
    client_secrets: Path,
    show_url: Callable[[str], None],
) -> tuple[str, str]:
    """One-time OAuth consent for a YouTube channel. Returns (channel title, sealed ref);
    the refresh token never leaves this function unencrypted."""
    box = build_secret_box(settings)
    if box is None:
        raise PipelineError("set CLIPFORGE_SECRET_KEY first (generate one: clipforge secret-key)")
    client_id, client_secret = read_client_secrets(client_secrets)
    creds = loopback_consent(client_id, client_secret, show_url)
    title = YouTubePublisher().channel_title(creds)
    return title, box.seal(workspace_id, creds.dumps())


def build_resolvers(settings: Settings) -> list[SourceResolver]:
    return [YouTubeResolver(), TwitchResolver(), KickResolver()]


def build_container(settings: Settings) -> Container:
    return Container(
        settings=settings,
        clock=SystemClock(),
        blobs=LocalBlobStore(settings.blob_root),
        events=InProcessEventBus(),
        quota=AllowAllQuota(),
        resolvers=build_resolvers(settings),
        fetcher=YtDlpFetcher(),
        transcriber=FasterWhisperTranscriber(
            model=settings.whisper_model,
            device=settings.whisper_device,
            compute_type=settings.whisper_compute_type,
        ),
    )


@lru_cache
def get_container() -> Container:
    return build_container(get_settings())


QUEUE_BASES: tuple[str, ...] = ("default", *sorted(set(QUEUE_FOR_KIND.values())))


def build_worker_broker(settings: Settings) -> Any:
    """Create and install the task broker for worker processes, declaring every queue."""
    return make_broker(settings.redis_url, install=True, queue_bases=QUEUE_BASES)


__all__ = [
    "DISPATCH_ACTOR",
    "QUEUE_BASES",
    "QUEUE_FOR_KIND",
    "Container",
    "build_container",
    "build_worker_broker",
    "get_container",
]
