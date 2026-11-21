"""LLMJudge backed by the Anthropic Messages API.

- Structured outputs (``output_config.format`` JSON schema) plus a Pydantic validation pass.
- ``effort: low``: this is short classification work, and lower effort is cheaper and faster.
- No temperature/top_p (rejected by current models) and no assistant prefill (400 on current
  models).
- The SDK retries 408/409/429/5xx and connection errors with backoff (``max_retries``). Anything
  that still fails becomes JudgeUnavailable, so the pipeline keeps signal-only scores.
- The system prompt is frozen and marked cacheable; per-request data goes in the user turn.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

import anthropic
import structlog
from pydantic import BaseModel, Field, ValidationError, field_validator

from clipforge.ports.judge import (
    CATEGORIES,
    JudgeInput,
    JudgeResult,
    JudgeUnavailable,
    JudgeUsage,
    JudgeVerdict,
)
from clipforge.prompts import load

log = structlog.get_logger(__name__)

PROMPT_ID = "judge.moment.v1"

_VERDICT_PROPS: dict[str, Any] = {
    "index": {"type": "integer"},
    **{c: {"type": "number"} for c in CATEGORIES},
    "setup_payoff": {"type": "boolean"},
    "overall": {"type": "number"},
    "titles": {"type": "array", "items": {"type": "string"}},
    "hook": {"type": "string"},
    "reason": {"type": "string"},
}
OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": _VERDICT_PROPS,
                "required": list(_VERDICT_PROPS),
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}


def _clamp01(v: float) -> float:
    return min(1.0, max(0.0, float(v)))


class _Verdict(BaseModel):
    index: int
    funny: float
    shocking: float
    clutch: float
    emotional: float
    quotable: float
    setup_payoff: bool
    overall: float
    titles: list[str] = Field(default_factory=list)
    hook: str = ""
    reason: str = ""

    @field_validator("funny", "shocking", "clutch", "emotional", "quotable", "overall")
    @classmethod
    def _range(cls, v: float) -> float:
        return _clamp01(v)  # the schema can't express ranges; enforce them here


class _Output(BaseModel):
    verdicts: list[_Verdict]


def parse_output(text: str, valid_indexes: set[int]) -> list[JudgeVerdict]:
    """Validate the model's JSON. Unknown/duplicate indexes are dropped. Pure; unit-tested."""
    try:
        out = _Output.model_validate(json.loads(text))
    except (json.JSONDecodeError, ValidationError) as exc:
        raise JudgeUnavailable(f"judge output failed validation: {exc}") from exc
    seen: set[int] = set()
    verdicts: list[JudgeVerdict] = []
    for v in out.verdicts:
        if v.index not in valid_indexes or v.index in seen:
            continue
        seen.add(v.index)
        titles = tuple(t.strip()[:80] for t in v.titles if t.strip())[:3]
        verdicts.append(
            JudgeVerdict(
                index=v.index,
                scores={c: getattr(v, c) for c in CATEGORIES},
                setup_payoff=v.setup_payoff,
                overall=v.overall,
                titles=titles,
                hook=v.hook.strip()[:80],
                reason=v.reason.strip()[:400],
            )
        )
    return verdicts


def build_user_content(items: Sequence[JudgeInput]) -> str:
    """Windows as JSON (data, not prose): makes the data/instruction boundary explicit."""
    payload = {
        "windows": [
            {
                "index": it.index,
                "duration_s": round(it.end_s - it.start_s, 1),
                "transcript": it.transcript,
                "signal_hints": {k: round(v, 2) for k, v in sorted(it.hints.items())},
            }
            for it in items
        ]
    }
    return "Rate these windows:\n" + json.dumps(payload, ensure_ascii=False)


class AnthropicJudge:
    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        client: Any | None = None,
        timeout_s: float = 90.0,
        max_retries: int = 3,
        max_tokens: int = 8000,
    ) -> None:
        self._model = model
        self.max_tokens = max_tokens
        self._client = client or anthropic.Anthropic(
            api_key=api_key, timeout=timeout_s, max_retries=max_retries
        )
        self._system = load(PROMPT_ID)

    @property
    def enabled(self) -> bool:
        return True

    @property
    def model(self) -> str:
        return self._model

    @property
    def prompt_id(self) -> str:
        return PROMPT_ID

    def request_params(self, items: Sequence[JudgeInput]) -> dict[str, Any]:
        return {
            "model": self._model,
            "max_tokens": self.max_tokens,
            "system": [
                {"type": "text", "text": self._system, "cache_control": {"type": "ephemeral"}}
            ],
            "messages": [{"role": "user", "content": build_user_content(items)}],
            "output_config": {
                "effort": "low",
                "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},
            },
        }

    def judge(self, items: Sequence[JudgeInput]) -> JudgeResult:
        if not items:
            return JudgeResult([], JudgeUsage())
        try:
            resp = self._client.messages.create(**self.request_params(items))
        except anthropic.AuthenticationError as exc:
            raise JudgeUnavailable("Anthropic API key rejected (401)") from exc
        except anthropic.BadRequestError as exc:  # our request is wrong: don't retry
            log.error("judge_bad_request", error=str(exc)[:500])
            raise JudgeUnavailable(f"judge request rejected: {exc.message[:300]}") from exc
        except anthropic.RateLimitError as exc:
            raise JudgeUnavailable("rate limited after retries") from exc
        except anthropic.APITimeoutError as exc:
            raise JudgeUnavailable("judge request timed out") from exc
        except anthropic.APIConnectionError as exc:
            raise JudgeUnavailable(f"network error: {exc}") from exc
        except anthropic.APIStatusError as exc:
            raise JudgeUnavailable(f"API error {exc.status_code}") from exc

        request_id = getattr(resp, "_request_id", None)
        u = resp.usage
        usage = JudgeUsage(
            input_tokens=int(u.input_tokens or 0),
            output_tokens=int(u.output_tokens or 0),
            cache_read_tokens=int(getattr(u, "cache_read_input_tokens", 0) or 0),
            cache_write_tokens=int(getattr(u, "cache_creation_input_tokens", 0) or 0),
        )
        if resp.stop_reason == "refusal":
            category = getattr(getattr(resp, "stop_details", None), "category", None)
            raise JudgeUnavailable(f"judge declined (refusal, category={category})")
        if resp.stop_reason == "max_tokens":
            raise JudgeUnavailable("judge output truncated (max_tokens)")
        text = next((b.text for b in resp.content if b.type == "text"), "")
        verdicts = parse_output(text, {it.index for it in items})
        return JudgeResult(verdicts, usage, request_id)
