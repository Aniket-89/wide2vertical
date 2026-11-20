"""Contract tests: each port's fake and real adapter must pass the same assertions.

Real network/model adapters run only with CLIPFORGE_NETWORK_TESTS=1 (never in CI).
"""

from __future__ import annotations

import math
import os
import struct
import wave
from collections.abc import Callable, Iterator
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy.orm import Session

from clipforge.adapters.fakes.pipeline import FakeResolver, FakeTranscriber, InMemoryPipelineStore
from clipforge.adapters.resolvers.ytdlp import YouTubeResolver
from clipforge.adapters.transcribers.faster_whisper import FasterWhisperTranscriber
from clipforge.domain.values import (
    BlobKey,
    ClipPolicy,
    JobStatus,
    Platform,
    Stage,
    WorkspaceId,
)
from clipforge.infra.config import get_settings
from clipforge.infra.db.pipeline_store import SqlPipelineStore
from clipforge.infra.db.sources import SourceAdmin
from clipforge.infra.seed import seed
from clipforge.ports.pipeline import (
    AssetRecord,
    PipelineStore,
    ResolvedVideo,
    SourceResolver,
    Transcriber,
    Transcript,
    Word,
)

NETWORK = os.environ.get("CLIPFORGE_NETWORK_TESTS") == "1"
needs_network = pytest.mark.skipif(not NETWORK, reason="set CLIPFORGE_NETWORK_TESTS=1")
WS = WorkspaceId(UUID(get_settings().default_workspace_id))

RV = ResolvedVideo(
    Platform.TWITCH, "https://www.twitch.tv/videos/42", "42", "streamer", None, "t", 120.0
)

AddSource = Callable[[str, ClipPolicy], None]


# ================================================================ PipelineStore
@pytest.fixture(params=["memory", "sql"])
def store_and_add(
    request: pytest.FixtureRequest,
) -> Iterator[tuple[PipelineStore, AddSource]]:
    if request.param == "memory":
        mem = InMemoryPipelineStore(WS)

        def add_mem(handle: str, policy: ClipPolicy) -> None:
            mem.add_source(Platform.TWITCH, handle, policy)

        yield mem, add_mem
    else:
        db: Session = request.getfixturevalue("db")
        seed(db, get_settings())

        def add_sql(handle: str, policy: ClipPolicy) -> None:
            SourceAdmin(db, WS).add(Platform.TWITCH, handle, policy)

        yield SqlPipelineStore(db, WS), add_sql


def test_store_source_lookup_is_normalised(store_and_add: tuple[PipelineStore, AddSource]) -> None:
    store, add = store_and_add
    add("@StreamER", ClipPolicy.OPEN_POLICY)
    s = store.find_source(Platform.TWITCH, "streamer", None)
    assert s is not None and s.clip_policy is ClipPolicy.OPEN_POLICY
    assert store.find_source(Platform.YOUTUBE, "streamer", None) is None
    assert store.find_source(Platform.TWITCH, "other", None) is None


def test_store_upsert_video_is_idempotent(store_and_add: tuple[PipelineStore, AddSource]) -> None:
    store, _ = store_and_add
    v1, c1 = store.upsert_video(RV, None, False)
    v2, c2 = store.upsert_video(RV, None, False)
    assert (c1, c2) == (True, False)
    assert v1.id == v2.id
    got = store.get_video(v1.id)
    assert got is not None and got.external_id == "42"


def test_store_assets_and_transcripts(store_and_add: tuple[PipelineStore, AddSource]) -> None:
    store, _ = store_and_add
    v, _ = store.upsert_video(RV, None, False)
    assert store.find_asset(v.id, "audio") is None
    a = AssetRecord(BlobKey.for_workspace(WS, "videos", str(v.id), "audio.m4a"), "h1", 10)
    store.save_asset(v.id, "audio", a)
    store.save_asset(v.id, "audio", a)  # idempotent
    assert store.find_asset(v.id, "audio") == a

    t = Transcript("en", 2.0, "m", (Word("hi", 0.0, 0.5, 0.9),))
    assert not store.has_transcript(v.id, "h1", "cfg")
    store.save_transcript(v.id, "h1", "cfg", t)
    store.save_transcript(v.id, "h1", "cfg", t)  # idempotent
    assert store.has_transcript(v.id, "h1", "cfg")
    assert not store.has_transcript(v.id, "h1", "cfg2")


def test_store_stage_tracking(store_and_add: tuple[PipelineStore, AddSource]) -> None:
    store, _ = store_and_add
    v, _ = store.upsert_video(RV, None, False)
    r1 = store.begin_stage(v.id, Stage.FETCH, "x")
    store.end_stage(r1, JobStatus.FAILED, "boom")
    r2 = store.begin_stage(v.id, Stage.FETCH, "x")
    store.end_stage(r2, JobStatus.SUCCEEDED)
    assert r1 != r2
    store.set_video_status(v.id, "fetch_done")
    got = store.get_video(v.id)
    assert got is not None and got.status == "fetch_done"


# ================================================================ SourceResolver
YT_URL = "https://www.youtube.com/watch?v=jNQXAC9IVRw"


@pytest.fixture(params=["fake", pytest.param("real", marks=needs_network)])
def yt_resolver(request: pytest.FixtureRequest) -> SourceResolver:
    if request.param == "fake":
        return FakeResolver(Platform.YOUTUBE, {YT_URL: "@jawed"}, duration_s=19)
    return YouTubeResolver()


def test_resolver_contract(yt_resolver: SourceResolver) -> None:
    assert yt_resolver.supports(YT_URL)
    assert not yt_resolver.supports("https://www.twitch.tv/videos/1")
    rv = yt_resolver.resolve(YT_URL)
    assert rv.platform is Platform.YOUTUBE
    assert rv.external_id == "jNQXAC9IVRw"
    assert rv.channel_handle == "jawed"  # normalised: no "@", lowercase
    assert rv.duration_s and rv.duration_s > 0


# ================================================================ Transcriber
def _tone_wav(path: Path, seconds: float = 2.0, rate: int = 16000) -> Path:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        frames = b"".join(
            struct.pack("<h", int(3000 * math.sin(2 * math.pi * 440 * i / rate)))
            for i in range(int(seconds * rate))
        )
        w.writeframes(frames)
    return path


@pytest.fixture(params=["fake", pytest.param("real", marks=needs_network)])
def transcriber(request: pytest.FixtureRequest) -> Transcriber:
    if request.param == "fake":
        return FakeTranscriber()
    return FasterWhisperTranscriber(model="tiny", device="cpu", compute_type="int8")


def test_transcriber_contract(transcriber: Transcriber, tmp_path: Path) -> None:
    t = transcriber.transcribe(_tone_wav(tmp_path / "tone.wav"))
    assert transcriber.config_version
    assert t.duration_s > 0
    starts = [w.start for w in t.words]
    assert starts == sorted(starts)
    assert all(w.end >= w.start for w in t.words)
