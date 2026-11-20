"""AnalysisStore and ChatProvider contracts: fake and real must behave the same."""

from __future__ import annotations

import os
from collections.abc import Iterator
from uuid import UUID

import numpy as np
import pytest
from sqlalchemy.orm import Session

from clipforge.adapters.chat.twitch_gql import TwitchGqlChatReplay
from clipforge.adapters.fakes.pipeline import FakeChatProvider, InMemoryAnalysisStore
from clipforge.domain.ids import new_id
from clipforge.domain.values import Platform, WorkspaceId
from clipforge.infra.config import get_settings
from clipforge.infra.db.pipeline_store import SqlPipelineStore
from clipforge.infra.seed import seed
from clipforge.ports.analysis import (
    CandidateWindow,
    ChatMessage,
    ChatProvider,
    ScoringProfileRecord,
    SignalSeries,
)
from clipforge.ports.pipeline import ResolvedVideo

WS = WorkspaceId(UUID(get_settings().default_workspace_id))
RV = ResolvedVideo(Platform.TWITCH, "https://www.twitch.tv/videos/42", "42", "s", None, "t", 60.0)


@pytest.fixture(params=["memory", "sql"])
def store(request: pytest.FixtureRequest) -> Iterator[InMemoryAnalysisStore | SqlPipelineStore]:
    if request.param == "memory":
        prof = ScoringProfileRecord(new_id(), "default", 1, {"a": 1.0}, {"x": 1})
        yield InMemoryAnalysisStore(WS, profiles=[prof])
    else:
        db: Session = request.getfixturevalue("db")
        seed(db, get_settings())
        yield SqlPipelineStore(db, WS)


def test_profile_lookup(store: InMemoryAnalysisStore | SqlPipelineStore) -> None:
    default = store.get_scoring_profile(None)
    assert default.name == "default"
    assert store.get_scoring_profile("default").id == default.id
    with pytest.raises(Exception, match="not found"):
        store.get_scoring_profile("nope")


def test_signal_roundtrip_is_idempotent(store: InMemoryAnalysisStore | SqlPipelineStore) -> None:
    v, _ = store.upsert_video(RV, None, False)
    s = SignalSeries("audio_energy", "1", np.array([0.0, 0.25, 1.0]), {"k": 1})
    assert store.find_signal(v.id, "audio_energy", "h") is None
    store.save_signal(v.id, s, "h")
    store.save_signal(v.id, s, "h")
    got = store.find_signal(v.id, "audio_energy", "h")
    assert got is not None and got.values.tolist() == [0.0, 0.25, 1.0] and got.meta == {"k": 1}
    assert store.find_signal(v.id, "audio_energy", "other") is None


def test_candidates_replace_and_rank(store: InMemoryAnalysisStore | SqlPipelineStore) -> None:
    v, _ = store.upsert_video(RV, None, False)
    prof = store.get_scoring_profile(None)
    w1 = CandidateWindow(0, 25, 10, 0.9, {"a": 0.9}, "r1")
    w2 = CandidateWindow(30, 55, 40, 0.5, {"a": 0.5}, "r2")
    assert store.replace_candidates(v.id, prof.id, [w1, w2]) == 2
    assert store.replace_candidates(v.id, prof.id, [w2]) == 1  # pending ones are replaced
    rows = store.list_candidates(v.id)
    assert [(c.start_s, c.rank, c.status) for c in rows] == [(30, 1, "pending")]


NETWORK = os.environ.get("CLIPFORGE_NETWORK_TESTS") == "1"
CHAT_LIVE = NETWORK and os.environ.get("CLIPFORGE_TWITCH_CHAT_TEST_VOD")


@pytest.fixture(
    params=[
        "fake",
        pytest.param(
            "real",
            marks=pytest.mark.skipif(
                not CHAT_LIVE,
                reason="set CLIPFORGE_NETWORK_TESTS=1 and CLIPFORGE_TWITCH_CHAT_TEST_VOD",
            ),
        ),
    ]
)
def chat_provider(request: pytest.FixtureRequest) -> tuple[ChatProvider, ResolvedVideo]:
    if request.param == "fake":
        msgs = [ChatMessage(float(i), "h", "hi") for i in range(5)]
        return FakeChatProvider(Platform.TWITCH, msgs), RV
    vod = str(CHAT_LIVE)
    rv = ResolvedVideo(
        Platform.TWITCH, f"https://www.twitch.tv/videos/{vod}", vod, "", None, "", None
    )
    return TwitchGqlChatReplay("contract", max_pages=3), rv


def test_chat_provider_contract(chat_provider: tuple[ChatProvider, ResolvedVideo]) -> None:
    provider, rv = chat_provider
    msgs = []
    try:
        for m in provider.fetch_replay(rv):
            msgs.append(m)
            if len(msgs) >= 200:
                break
    except Exception as exc:  # the contract allows only ChatUnavailable as failure
        from clipforge.ports.analysis import ChatUnavailable

        assert isinstance(exc, ChatUnavailable)
        return
    times = [m.t_s for m in msgs]
    assert times == sorted(times)
    assert all(len(m.author) == 16 or m.author == "h" for m in msgs)  # hashed, not raw names
