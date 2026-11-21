"""OpenAICompatJudge (OpenRouter / Groq / any OpenAI-compatible endpoint) with a fake client."""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import httpx2
import openai
import pytest
from pydantic import SecretStr

from clipforge.adapters.judges.anthropic_judge import OUTPUT_SCHEMA, AnthropicJudge
from clipforge.adapters.judges.null_judge import NullJudge
from clipforge.adapters.judges.openai_compat_judge import (
    PROVIDER_BASE_URLS,
    OpenAICompatJudge,
    _strip_fences,
)
from clipforge.infra.config import Settings
from clipforge.infra.registry import build_judge
from clipforge.ports.judge import JudgeInput, JudgeUnavailable

ITEMS = [
    JudgeInput(0, 0.0, 30.0, "no way!", {"audio_energy": 0.8}),
    JudgeInput(1, 60.0, 90.0, "anyway", {}),
]
REQ = httpx2.Request("POST", "https://api.groq.com/openai/v1/chat/completions")


def verdicts_json(*idx: int) -> str:
    return json.dumps(
        {
            "verdicts": [
                {
                    "index": i,
                    "funny": 0.5,
                    "shocking": 0.1,
                    "clutch": 0.1,
                    "emotional": 0.3,
                    "quotable": 0.2,
                    "setup_payoff": True,
                    "overall": 0.7 - i * 0.5,
                    "titles": ["t1", "t2", "t3"],
                    "hook": "h",
                    "reason": "r",
                }
                for i in idx
            ]
        }
    )


def completion(text: str, finish: str = "stop", refusal: str | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        id="gen-123",
        choices=[
            SimpleNamespace(
                finish_reason=finish, message=SimpleNamespace(content=text, refusal=refusal)
            )
        ],
        usage=SimpleNamespace(
            prompt_tokens=700,
            completion_tokens=150,
            prompt_tokens_details=SimpleNamespace(cached_tokens=200),
        ),
    )


class FakeCompletions:
    def __init__(self, results: list[Any]) -> None:
        self.results = results
        self.calls: list[dict[str, Any]] = []

    def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        r = self.results.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


def make(*results: Any, provider: str = "groq") -> tuple[OpenAICompatJudge, FakeCompletions]:
    comp = FakeCompletions(list(results))
    client = SimpleNamespace(chat=SimpleNamespace(completions=comp))
    return OpenAICompatJudge(provider=provider, model="some-model", client=client), comp


def bad_request(msg: str) -> openai.BadRequestError:
    return openai.BadRequestError(
        msg, response=httpx2.Response(400, request=REQ), body={"error": msg}
    )


def test_request_uses_strict_json_schema_and_system_prompt() -> None:
    j, comp = make(completion(verdicts_json(0, 1)))
    res = j.judge(ITEMS)
    req = comp.calls[0]
    assert req["model"] == "some-model"
    assert req["response_format"]["type"] == "json_schema"
    assert req["response_format"]["json_schema"]["schema"] == OUTPUT_SCHEMA
    assert [m["role"] for m in req["messages"]] == ["system", "user"]
    assert [v.index for v in res.verdicts] == [0, 1]
    assert res.usage.input_tokens == 700 and res.usage.cache_read_tokens == 200
    assert j.model == "groq:some-model"  # provider is part of provenance and cache key


def test_falls_back_to_json_mode_once_when_schema_unsupported() -> None:
    j, comp = make(
        bad_request("response_format json_schema is not supported for this model"),
        completion("```json\n" + verdicts_json(0) + "\n```"),  # fenced output gets stripped
        completion(verdicts_json(0, 1)),
    )
    assert [v.index for v in j.judge(ITEMS).verdicts] == [0]
    assert comp.calls[1]["response_format"] == {"type": "json_object"}
    assert "single JSON object" in comp.calls[1]["messages"][0]["content"]
    j.judge(ITEMS)  # later calls go straight to JSON mode
    assert len(comp.calls) == 3 and comp.calls[2]["response_format"] == {"type": "json_object"}


def test_other_bad_requests_are_not_retried() -> None:
    j, comp = make(bad_request("model not found"))
    with pytest.raises(JudgeUnavailable, match="rejected"):
        j.judge(ITEMS)
    assert len(comp.calls) == 1 and j.json_schema_supported


@pytest.mark.parametrize(
    ("resp", "match"),
    [
        (completion("{}", finish="length"), "truncated"),
        (completion("{}", finish="content_filter"), "declined"),
        (completion("", refusal="I can't help with that"), "declined"),
        (completion("not json"), "validation"),
        (SimpleNamespace(id="x", choices=[], usage=None), "no choices"),
    ],
)
def test_bad_responses_raise(resp: Any, match: str) -> None:
    j, _ = make(resp)
    with pytest.raises(JudgeUnavailable, match=match):
        j.judge(ITEMS)


@pytest.mark.parametrize(
    ("exc", "match"),
    [
        (openai.APITimeoutError(request=REQ), "timed out"),
        (openai.APIConnectionError(request=REQ), "network"),
        (
            openai.RateLimitError("x", response=httpx2.Response(429, request=REQ), body=None),
            "rate limited",
        ),
        (
            openai.AuthenticationError("x", response=httpx2.Response(401, request=REQ), body=None),
            "key rejected",
        ),
        (
            openai.InternalServerError("x", response=httpx2.Response(503, request=REQ), body=None),
            "API error 503",
        ),
    ],
)
def test_sdk_errors_become_judge_unavailable(exc: Exception, match: str) -> None:
    j, _ = make(exc)
    with pytest.raises(JudgeUnavailable, match=match):
        j.judge(ITEMS)


def test_strip_fences() -> None:
    assert _strip_fences('```json\n{"a":1}\n```') == '{"a":1}'
    assert _strip_fences('{"a":1}') == '{"a":1}'


def test_known_provider_urls_and_unknown_needs_base_url() -> None:
    assert PROVIDER_BASE_URLS["groq"].startswith("https://api.groq.com")
    assert PROVIDER_BASE_URLS["openrouter"].startswith("https://openrouter.ai")
    with pytest.raises(ValueError, match="BASE_URL"):
        OpenAICompatJudge(provider="openai_compat", model="m", api_key="k")


# ---------------------------------------------------------------- registry selection
def settings(**kw: object) -> Settings:
    return Settings(env="test", _env_file=None, enable_llm_judge=True, **kw)  # type: ignore[arg-type, call-arg]


def test_registry_picks_adapter_per_provider() -> None:
    key = SecretStr("key-abcdef-1")
    assert isinstance(build_judge(settings(anthropic_api_key=key)), AnthropicJudge)
    g = build_judge(settings(llm_provider="groq", llm_api_key=key, llm_model="llama-x"))
    assert isinstance(g, OpenAICompatJudge) and g.model == "groq:llama-x"
    o = build_judge(settings(llm_provider="openrouter", llm_api_key=key, llm_model="a/b"))
    assert isinstance(o, OpenAICompatJudge) and o.model == "openrouter:a/b"


@pytest.mark.parametrize(
    ("kw", "reason"),
    [
        ({"llm_provider": "groq", "llm_model": "llama-x"}, "CLIPFORGE_LLM_API_KEY is empty"),
        ({"llm_provider": "groq", "llm_api_key": SecretStr("k" * 10)}, "set CLIPFORGE_LLM_MODEL"),
        (
            {"llm_provider": "openai_compat", "llm_api_key": SecretStr("k" * 10), "llm_model": "m"},
            "needs CLIPFORGE_LLM_BASE_URL",
        ),
    ],
)
def test_registry_misconfiguration_falls_back_to_null(kw: dict[str, object], reason: str) -> None:
    j = build_judge(settings(**kw))
    assert isinstance(j, NullJudge) and reason in j.reason
