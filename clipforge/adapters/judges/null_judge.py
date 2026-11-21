"""NullJudge: no LLM. The judge stage is skipped and candidates keep signal-only scores."""

from __future__ import annotations

from collections.abc import Sequence

from clipforge.ports.judge import JudgeInput, JudgeResult, JudgeUsage


class NullJudge:
    def __init__(self, reason: str = "LLM judge disabled") -> None:
        self.reason = reason

    @property
    def enabled(self) -> bool:
        return False

    @property
    def model(self) -> str:
        return "none"

    @property
    def prompt_id(self) -> str:
        return "none"

    def judge(self, items: Sequence[JudgeInput]) -> JudgeResult:
        return JudgeResult([], JudgeUsage())
