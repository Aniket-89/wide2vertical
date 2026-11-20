"""Composition root: the only place that imports concrete adapters.

Config picks adapters; everything else receives ports.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from clipforge.adapters.audio.pyav_rms import PyAvRmsAnalyzer
from clipforge.adapters.blobstore.local import LocalBlobStore
from clipforge.adapters.chat.twitch_gql import TwitchGqlChatReplay
from clipforge.adapters.chat.youtube_live_chat import YouTubeLiveChatReplay
from clipforge.adapters.eventbus.in_process import InProcessEventBus
from clipforge.adapters.fetchers.ytdlp import YtDlpFetcher
from clipforge.adapters.misc import AllowAllQuota, SystemClock
from clipforge.adapters.queue.dramatiq_queue import DISPATCH_ACTOR, DramatiqJobQueue, make_broker
from clipforge.adapters.resolvers.ytdlp import KickResolver, TwitchResolver, YouTubeResolver
from clipforge.adapters.transcribers.faster_whisper import FasterWhisperTranscriber
from clipforge.domain.values import Platform, WorkspaceId
from clipforge.infra.config import Settings, get_settings
from clipforge.infra.db.pipeline_store import SqlPipelineStore
from clipforge.ports.analysis import AudioAnalyzer, ChatProvider
from clipforge.ports.infra import BlobStore, Clock, EventBus, JobQueue, QuotaService
from clipforge.ports.pipeline import MediaFetcher, SourceResolver, Transcriber
from clipforge.services.analyze import AnalyzePipeline
from clipforge.services.ingest import IngestPipeline
from clipforge.signals import default_signals

# Job kinds → base queue. Transcription has its own queue family so GPU workers can own it.
QUEUE_FOR_KIND: dict[str, str] = {"pipeline.transcribe": "transcribe"}


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
        )


def build_chat_providers(settings: Settings, salt: str) -> dict[Platform, ChatProvider]:
    """Unofficial chat adapters are opt-in per platform (ADR-0006). Kick: not built yet."""
    providers: dict[Platform, ChatProvider] = {}
    if settings.enable_twitch_chat_gql:
        providers[Platform.TWITCH] = TwitchGqlChatReplay(salt)
    if settings.enable_youtube_chat_replay:
        providers[Platform.YOUTUBE] = YouTubeLiveChatReplay(salt)
    return providers


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
