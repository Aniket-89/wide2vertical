"""LLMJudge port (M3). The pipeline must work with NullJudge (no LLM at all)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol

from clipforge.ports.pipeline import PipelineError

CATEGORIES = ("funny", "shocking", "clutch", "emotional", "quotable")


class JudgeUnavailable(PipelineError):
    """This judge call failed (timeout, rate limit, refusal, bad output). Never fatal: the
    affected windows keep their signal-only score."""


@dataclass(frozen=True, slots=True)
class JudgeInput:
    index: int  # stable id within one call
    start_s: float
    end_s: float
    transcript: str
    hints: Mapping[str, float] = field(default_factory=dict)  # signal components, 0..1


@dataclass(frozen=True, slots=True)
class JudgeVerdict:
    index: int
    scores: Mapping[str, float]  # CATEGORIES, each 0..1
    setup_payoff: bool
    overall: float  # 0..1
    titles: tuple[str, ...]
    hook: str
    reason: str


@dataclass(frozen=True, slots=True)
class JudgeUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def __add__(self, o: JudgeUsage) -> JudgeUsage:
        return JudgeUsage(
            self.input_tokens + o.input_tokens,
            self.output_tokens + o.output_tokens,
            self.cache_read_tokens + o.cache_read_tokens,
            self.cache_write_tokens + o.cache_write_tokens,
        )


@dataclass(frozen=True, slots=True)
class JudgeResult:
    verdicts: Sequence[JudgeVerdict]
    usage: JudgeUsage
    request_id: str | None = None


class LLMJudge(Protocol):
    @property
    def enabled(self) -> bool:
        """False for NullJudge: the pipeline skips the judge stage entirely."""
        ...

    @property
    def model(self) -> str: ...

    @property
    def prompt_id(self) -> str: ...

    def judge(self, items: Sequence[JudgeInput]) -> JudgeResult:
        """Rate a batch of windows in one call. Raise JudgeUnavailable on failure. Verdicts may
        be missing for some indexes; callers treat those as not judged."""
        ...
