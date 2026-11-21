"""LLMJudge contract: FakeJudge and AnthropicJudge must behave the same.

The real judge calls a paid API, so it runs only with CLIPFORGE_NETWORK_TESTS=1 and a key
in CLIPFORGE_ANTHROPIC_API_KEY. Never in CI.
"""

from __future__ import annotations

import os

import pytest

from clipforge.adapters.fakes.pipeline import FakeJudge
from clipforge.adapters.judges.anthropic_judge import AnthropicJudge
from clipforge.ports.judge import CATEGORIES, JudgeInput, LLMJudge

KEY = os.environ.get("CLIPFORGE_ANTHROPIC_API_KEY", "")
LIVE = os.environ.get("CLIPFORGE_NETWORK_TESTS") == "1" and bool(KEY)

ITEMS = [
    JudgeInput(
        0,
        0.0,
        28.0,
        "wait wait WAIT no way he just hit that shot from across the "
        "map! dude that's insane, chat did you see that?!",
        {"chat_velocity": 0.9},
    ),
    JudgeInput(
        1,
        300.0,
        330.0,
        "okay so now I'm going to go to the menu and change the settings, give me a second here",
        {"audio_energy": 0.1},
    ),
]


@pytest.fixture(
    params=[
        "fake",
        pytest.param(
            "real",
            marks=pytest.mark.skipif(
                not LIVE,
                reason="paid API: set CLIPFORGE_NETWORK_TESTS=1 and CLIPFORGE_ANTHROPIC_API_KEY",
            ),
        ),
    ]
)
def judge(request: pytest.FixtureRequest) -> LLMJudge:
    if request.param == "fake":
        return FakeJudge(lambda it: 0.9 if it.index == 0 else 0.1)
    return AnthropicJudge(
        model=os.environ.get("CLIPFORGE_LLM_MODEL", "claude-haiku-5-5"), api_key=KEY
    )


def test_judge_contract(judge: LLMJudge) -> None:
    res = judge.judge(ITEMS)
    assert judge.enabled and judge.model and judge.prompt_id == "judge.moment.v1"
    by_index = {v.index: v for v in res.verdicts}
    assert set(by_index) <= {0, 1} and by_index  # at least one verdict, no unknown indexes
    for v in res.verdicts:
        assert 0.0 <= v.overall <= 1.0
        assert set(v.scores) == set(CATEGORIES)
        assert all(0.0 <= s <= 1.0 for s in v.scores.values())
        assert len(v.titles) <= 3
    if {0, 1} <= set(by_index):  # the hype moment should beat the menu-fiddling one
        assert by_index[0].overall > by_index[1].overall
    assert res.usage.input_tokens >= 0
