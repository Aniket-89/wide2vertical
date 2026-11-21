# LLM prompts (versioned)

Every prompt sent to an `LLMJudge` lives in `clipforge/prompts/` as one file per version. It is
indexed here with a content fingerprint, and a test fails if the two drift apart. **Never edit
a released prompt.** Add `…v2`, point the adapter at it, and keep v1 so old verdicts stay
explainable. Every stored verdict records its `prompt_id` and `model`.

| id | file | fingerprint | used by |
|---|---|---|---|
| `judge.moment.v1` | `clipforge/prompts/judge_moment_v1.md` | fingerprint `46fa020b5103` | `AnthropicJudge`, `OpenAICompatJudge` (M3) |

## `judge.moment.v1`

**Purpose:** rate candidate windows for standalone clip potential; suggest titles and a hook.

**Request shape** (`clipforge/adapters/judges/anthropic_judge.py`):
- model `CLIPFORGE_LLM_MODEL` (default `claude-haiku-5-5`, a cheap model for scoring)
- `output_config.effort: "low"`, plus `output_config.format` as a strict JSON schema
  (`verdicts[]`: `index`, `funny`, `shocking`, `clutch`, `emotional`, `quotable`, `setup_payoff`,
  `overall`, `titles[]`, `hook`, `reason`); ranges are enforced by Pydantic after parsing
- system prompt = this file, marked `cache_control: ephemeral`; the user turn holds windows as
  JSON (`index`, `duration_s`, `transcript`, `signal_hints`)
- up to `CLIPFORGE_JUDGE_BATCH_SIZE` (8) windows per request; only the top
  `judge_top_n_per_hour` (profile param, default 30) windows per video hour are judged
- no `temperature`/`top_p` (rejected by current models), no assistant prefill

**Prompt-injection stance:** transcripts are third-party speech. The prompt tells the model to
treat them as data, and they arrive as JSON values, not prose. The judge's influence is bounded:
it is one weighted component (`llm_moment_score`, ≈21% by default) and titles are suggestions a
human reviews.

**On OpenAI-compatible providers** (OpenRouter, Groq, ...): the same text is the system message,
sent with `response_format: json_schema` (strict). Where a model lacks schema support, it's sent
with `json_object` and a one-paragraph JSON shape appended (see `_JSON_MODE_SUFFIX` in
`openai_compat_judge.py`). That suffix is part of the request, not a new prompt version.
