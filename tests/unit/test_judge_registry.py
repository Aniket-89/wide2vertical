"""The paid judge is used only when explicitly enabled AND a key is configured."""

from __future__ import annotations

from pydantic import SecretStr

from clipforge.adapters.judges.anthropic_judge import AnthropicJudge
from clipforge.adapters.judges.null_judge import NullJudge
from clipforge.infra.config import Settings
from clipforge.infra.registry import build_judge, build_judge_config


def settings(**kw: object) -> Settings:
    return Settings(env="test", _env_file=None, **kw)  # type: ignore[arg-type, call-arg]


def test_disabled_by_default() -> None:
    j = build_judge(settings())
    assert isinstance(j, NullJudge) and "off" in j.reason


def test_enabled_without_key_stays_null() -> None:
    j = build_judge(settings(enable_llm_judge=True))
    assert isinstance(j, NullJudge) and "empty" in j.reason


def test_enabled_with_key_builds_anthropic_judge_once() -> None:
    s = settings(enable_llm_judge=True, anthropic_api_key=SecretStr("sk-ant-test-123456"))
    a, b = build_judge(s), build_judge(s)
    assert isinstance(a, AnthropicJudge) and a is b  # one HTTP client per process
    assert a.model == "claude-haiku-5-5"
    assert "sk-ant" not in repr(s)  # the key never shows up in settings dumps/logs


def test_judge_config_from_settings() -> None:
    cfg = build_judge_config(settings(judge_batch_size=5, llm_price_in_per_mtok=1.0))
    assert cfg.batch_size == 5 and cfg.price_in_per_mtok == 1.0
