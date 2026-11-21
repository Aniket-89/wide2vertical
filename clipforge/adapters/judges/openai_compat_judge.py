"""LLMJudge for any OpenAI-compatible Chat Completions endpoint: OpenRouter, Groq, or a
self-hosted server (vLLM, Ollama, ...) via a base-URL override.

It reuses the same versioned prompt, JSON schema and Pydantic validation as AnthropicJudge,
so verdicts are identical in shape whichever provider produced them (ADR-0030).

Structured output support varies by provider *and* model: the adapter asks for a strict
``json_schema`` first and, if the endpoint rejects ``response_format``, falls back once to
``json_object`` (plain JSON mode) for the rest of the process. Validation catches anything
off-schema either way.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import openai
import structlog

from clipforge.adapters.judges.anthropic_judge import (
    OUTPUT_SCHEMA,
    PROMPT_ID,
    build_user_content,
    parse_output,
)
from clipforge.ports.judge import JudgeInput, JudgeResult, JudgeUnavailable, JudgeUsage
from clipforge.prompts import load

log = structlog.get_logger(__name__)

PROVIDER_BASE_URLS = {
    "openrouter": "https://openrouter.ai/api/v1",
    "groq": "https://api.groq.com/openai/v1",
}

# Appended in JSON-object mode, where the schema isn't enforced by the endpoint.
_JSON_MODE_SUFFIX = (
    "\n\nRespond with a single JSON object only, no prose, of the form "
    '{"verdicts": [{"index": int, "funny": number, "shocking": number, "clutch": number, '
    '"emotional": number, "quotable": number, "setup_payoff": bool, "overall": number, '
    '"titles": [string, string, string], "hook": string, "reason": string}]}'
)


class OpenAICompatJudge:
    def __init__(
        self,
        *,
        provider: str,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        client: Any | None = None,
        timeout_s: float = 90.0,
        max_retries: int = 3,
        max_tokens: int = 8000,
    ) -> None:
        self.provider = provider
        self._model = model
        self.max_tokens = max_tokens
        url = base_url or PROVIDER_BASE_URLS.get(provider)
        if client is None and not url:
            raise ValueError(f"no base URL for provider {provider!r}; set CLIPFORGE_LLM_BASE_URL")
        # No attribution headers (OpenRouter's optional HTTP-Referer/X-Title): we don't send
        # anything that identifies the user or project to a third party.
        self._client = client or openai.OpenAI(
            api_key=api_key, base_url=url, timeout=timeout_s, max_retries=max_retries
        )
        self._system = load(PROMPT_ID)
        self.json_schema_supported = True  # flips to False after the first rejection

    @property
    def enabled(self) -> bool:
        return True

    @property
    def model(self) -> str:
        return f"{self.provider}:{self._model}"  # provenance + cache key include the provider

    @property
    def prompt_id(self) -> str:
        return PROMPT_ID

    def request_params(self, items: Sequence[JudgeInput]) -> dict[str, Any]:
        if self.json_schema_supported:
            fmt: dict[str, Any] = {
                "type": "json_schema",
                "json_schema": {"name": "clip_verdicts", "schema": OUTPUT_SCHEMA, "strict": True},
            }
            system = self._system
        else:
            fmt = {"type": "json_object"}
            system = self._system + _JSON_MODE_SUFFIX
        return {
            "model": self._model,
            "max_tokens": self.max_tokens,
            "temperature": 0,  # scoring should be as repeatable as the provider allows
            "response_format": fmt,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": build_user_content(items)},
            ],
        }

    def _create(self, items: Sequence[JudgeInput]) -> Any:
        try:
            return self._client.chat.completions.create(**self.request_params(items))
        except openai.BadRequestError as exc:
            if (
                self.json_schema_supported
                and "response_format" in str(exc).lower() + str(getattr(exc, "body", "")).lower()
            ):
                log.warning("judge_json_schema_unsupported_falling_back", model=self.model)
                self.json_schema_supported = False
                return self._client.chat.completions.create(**self.request_params(items))
            raise

    def judge(self, items: Sequence[JudgeInput]) -> JudgeResult:
        if not items:
            return JudgeResult([], JudgeUsage())
        try:
            resp = self._create(items)
        except openai.AuthenticationError as exc:
            raise JudgeUnavailable(f"{self.provider} API key rejected (401)") from exc
        except openai.BadRequestError as exc:
            log.error("judge_bad_request", provider=self.provider, error=str(exc)[:500])
            raise JudgeUnavailable(f"judge request rejected: {str(exc)[:300]}") from exc
        except openai.RateLimitError as exc:
            raise JudgeUnavailable("rate limited after retries") from exc
        except openai.APITimeoutError as exc:
            raise JudgeUnavailable("judge request timed out") from exc
        except openai.APIConnectionError as exc:
            raise JudgeUnavailable(f"network error: {exc}") from exc
        except openai.APIStatusError as exc:
            raise JudgeUnavailable(f"API error {exc.status_code}") from exc

        u = getattr(resp, "usage", None)
        details = getattr(u, "prompt_tokens_details", None)
        usage = JudgeUsage(
            input_tokens=int(getattr(u, "prompt_tokens", 0) or 0),
            output_tokens=int(getattr(u, "completion_tokens", 0) or 0),
            cache_read_tokens=int(getattr(details, "cached_tokens", 0) or 0),
        )
        if not resp.choices:
            raise JudgeUnavailable("judge returned no choices")
        choice = resp.choices[0]
        if choice.finish_reason == "length":
            raise JudgeUnavailable("judge output truncated (length)")
        if choice.finish_reason == "content_filter":
            raise JudgeUnavailable("judge declined (content_filter)")
        refusal = getattr(choice.message, "refusal", None)
        if refusal:
            raise JudgeUnavailable(f"judge declined: {str(refusal)[:200]}")
        text = choice.message.content or ""
        verdicts = parse_output(_strip_fences(text), {it.index for it in items})
        return JudgeResult(verdicts, usage, getattr(resp, "id", None))


def _strip_fences(text: str) -> str:
    """Some models wrap JSON-mode output in ```json fences despite instructions."""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t
        t = t.rsplit("```", 1)[0]
    return t.strip()
