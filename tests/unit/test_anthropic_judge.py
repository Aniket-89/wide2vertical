"""AnthropicJudge against a fake Messages client: request shape, parsing, failure mapping."""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx2
import pytest

from clipforge.adapters.judges.anthropic_judge import (
    OUTPUT_SCHEMA,
    PROMPT_ID,
    AnthropicJudge,
    build_user_content,
    parse_output,
)
from clipforge.adapters.judges.null_judge import NullJudge
from clipforge.ports.judge import JudgeInput, JudgeUnavailable
from clipforge.prompts import PROMPT_FILES, fingerprint, load

ITEMS = [
    JudgeInput(0, 10.0, 40.0, "no way that just happened!", {"chat_velocity": 0.91}),
    JudgeInput(1, 100.0, 130.0, "okay so anyway the next step", {"audio_energy": 0.2}),
]


def verdict(i: int, overall: float = 0.8, **kw: Any) -> dict[str, Any]:
    base = {
        "index": i,
        "funny": 0.9,
        "shocking": 0.2,
        "clutch": 0.1,
        "emotional": 0.4,
        "quotable": 0.7,
        "setup_payoff": True,
        "overall": overall,
        "titles": ["a", "b", "c"],
        "hook": "wait for it",
        "reason": "great payoff",
    }
    return {**base, **kw}


def response(text: str, stop: str = "end_turn", **usage: int) -> SimpleNamespace:
    return SimpleNamespace(
        content=[
            SimpleNamespace(type="thinking", thinking=""),
            SimpleNamespace(type="text", text=text),
        ],
        stop_reason=stop,
        stop_details=SimpleNamespace(category="cyber") if stop == "refusal" else None,
        usage=SimpleNamespace(
            input_tokens=usage.get("i", 500),
            output_tokens=usage.get("o", 120),
            cache_read_input_tokens=usage.get("cr", 0),
            cache_creation_input_tokens=usage.get("cw", 0),
        ),
        _request_id="req_test",
    )


class FakeMessages:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def judge_with(result: Any) -> tuple[AnthropicJudge, FakeMessages]:
    msgs = FakeMessages(result)
    return AnthropicJudge(model="claude-haiku-5-5", client=SimpleNamespace(messages=msgs)), msgs


# ---------------------------------------------------------------- request shape
def test_request_shape_follows_current_api() -> None:
    j, msgs = judge_with(response(json.dumps({"verdicts": [verdict(0), verdict(1, 0.1)]})))
    j.judge(ITEMS)
    req = msgs.calls[0]
    assert req["model"] == "claude-haiku-5-5"
    assert req["output_config"]["effort"] == "low"
    assert req["output_config"]["format"] == {"type": "json_schema", "schema": OUTPUT_SCHEMA}
    for banned in ("temperature", "top_p", "top_k", "thinking"):  # 400s on current models
        assert banned not in req
    assert req["messages"][-1]["role"] == "user"  # no assistant prefill
    assert req["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert req["system"][0]["text"] == load(PROMPT_ID)


def test_windows_are_sent_as_json_data() -> None:
    content = build_user_content(ITEMS)
    payload = json.loads(content.split("\n", 1)[1])
    assert payload["windows"][0] == {
        "index": 0,
        "duration_s": 30.0,
        "transcript": "no way that just happened!",
        "signal_hints": {"chat_velocity": 0.91},
    }


def test_schema_is_strict() -> None:
    item = OUTPUT_SCHEMA["properties"]["verdicts"]["items"]
    assert item["additionalProperties"] is False
    assert set(item["required"]) == set(item["properties"])


# ---------------------------------------------------------------- parsing
def test_parse_success_with_usage_and_request_id() -> None:
    j, _ = judge_with(response(json.dumps({"verdicts": [verdict(0), verdict(1, 0.1)]}), cr=900))
    res = j.judge(ITEMS)
    assert [v.index for v in res.verdicts] == [0, 1]
    assert res.verdicts[0].overall == 0.8 and res.verdicts[0].titles == ("a", "b", "c")
    assert res.usage.input_tokens == 500 and res.usage.cache_read_tokens == 900
    assert res.request_id == "req_test"


def test_parse_clamps_drops_unknown_and_duplicates() -> None:
    text = json.dumps(
        {
            "verdicts": [
                verdict(0, 1.7, funny=-3, titles=["1", "2", "3", "4", "  "]),
                verdict(0, 0.2),  # duplicate index
                verdict(9),  # not requested
            ]
        }
    )
    out = parse_output(text, {0, 1})
    assert len(out) == 1
    assert out[0].overall == 1.0 and out[0].scores["funny"] == 0.0
    assert out[0].titles == ("1", "2", "3")


@pytest.mark.parametrize("text", ["not json", json.dumps({"verdicts": [{"index": 0}]}), "{}"])
def test_invalid_output_raises(text: str) -> None:
    with pytest.raises(JudgeUnavailable, match="validation"):
        parse_output(text, {0})


# ---------------------------------------------------------------- failure mapping
@pytest.mark.parametrize(("stop", "match"), [("refusal", "declined"), ("max_tokens", "truncated")])
def test_stop_reasons_raise(stop: str, match: str) -> None:
    j, _ = judge_with(response("{}", stop=stop))
    with pytest.raises(JudgeUnavailable, match=match):
        j.judge(ITEMS)


REQ = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")


def status_error(cls: type[anthropic.APIStatusError], code: int) -> anthropic.APIStatusError:
    return cls("boom", response=httpx2.Response(code, request=REQ), body=None)


@pytest.mark.parametrize(
    ("exc", "match"),
    [
        (anthropic.APITimeoutError(request=REQ), "timed out"),
        (anthropic.APIConnectionError(request=REQ), "network"),
        (status_error(anthropic.RateLimitError, 429), "rate limited"),
        (status_error(anthropic.AuthenticationError, 401), "key rejected"),
        (status_error(anthropic.BadRequestError, 400), "rejected"),
        (status_error(anthropic.InternalServerError, 500), "API error 500"),
    ],
)
def test_sdk_errors_become_judge_unavailable(exc: Exception, match: str) -> None:
    j, _ = judge_with(exc)
    with pytest.raises(JudgeUnavailable, match=match):
        j.judge(ITEMS)


def test_empty_batch_makes_no_call() -> None:
    j, msgs = judge_with(response("{}"))
    assert j.judge([]).verdicts == [] and msgs.calls == []


def test_null_judge() -> None:
    n = NullJudge()
    assert not n.enabled and n.judge(ITEMS).verdicts == []


# ---------------------------------------------------------------- prompts registry
def test_every_prompt_is_indexed_in_docs_with_current_fingerprint() -> None:
    doc = (Path(__file__).parents[2] / "docs" / "PROMPTS.md").read_text(encoding="utf-8")
    for pid in PROMPT_FILES:
        m = re.search(rf"`{re.escape(pid)}`.*?fingerprint `([0-9a-f]{{12}})`", doc, re.S)
        assert m, f"{pid} missing from docs/PROMPTS.md"
        assert m.group(1) == fingerprint(pid), f"{pid} changed: add a new version instead"


def test_unknown_prompt_id() -> None:
    with pytest.raises(KeyError):
        load("judge.nope.v9")
