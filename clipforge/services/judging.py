"""M3: send the top windows to the LLM judge and blend its verdict into the ranking.

Cost control is the point of this module: only the top ``judge_top_n_per_hour`` windows go to
the judge (never the full transcript), windows are batched, and verdicts are cached by content,
so re-analysis with an unchanged window costs nothing. Failure isolation: one failed batch leaves
those windows on their signal score; it never fails the video.
"""

from __future__ import annotations

import json
import math
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import structlog

from clipforge.domain.values import BlobKey, WorkspaceId
from clipforge.ports.infra import BlobStore
from clipforge.ports.judge import (
    CATEGORIES,
    JudgeInput,
    JudgeUnavailable,
    JudgeUsage,
    JudgeVerdict,
    LLMJudge,
)
from clipforge.ports.pipeline import Word
from clipforge.services._common import sha256_text

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class JudgeConfig:
    batch_size: int = 8
    max_windows_per_video: int = 150  # hard cap on top of judge_top_n_per_hour
    max_transcript_chars: int = 2000
    price_in_per_mtok: float = 0.10  # claude-haiku-5-5, prompts <= 100K tokens
    price_out_per_mtok: float = 0.50
    price_cache_read_per_mtok: float = 0.01
    price_cache_write_per_mtok: float = 0.125

    def cost_usd(self, u: JudgeUsage) -> float:
        return (
            u.input_tokens * self.price_in_per_mtok
            + u.output_tokens * self.price_out_per_mtok
            + u.cache_read_tokens * self.price_cache_read_per_mtok
            + u.cache_write_tokens * self.price_cache_write_per_mtok
        ) / 1_000_000


@dataclass(frozen=True)
class JudgeTarget:
    """A window to judge, independent of whether it's a stored candidate or in memory."""

    key: Any  # caller's id (candidate id, or list index in harness mode)
    start_s: float
    end_s: float
    signal_score: float
    components: Mapping[str, Any]
    reason: str


@dataclass
class JudgeOutcome:
    verdicts: dict[Any, JudgeVerdict] = field(default_factory=dict)  # by target key
    usage: JudgeUsage = field(default_factory=JudgeUsage)
    requested: int = 0
    from_cache: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.requested == 0:
            return "nothing to judge"
        judged = len(self.verdicts)
        if judged == 0:
            return f"unavailable: {self.errors[0] if self.errors else 'no verdicts'}"
        tag = "ok" if judged == self.requested else "partial"
        return f"{tag}: {judged}/{self.requested} judged ({self.from_cache} cached)"


def window_transcript(words: Sequence[Word], start_s: float, end_s: float, cap: int) -> str:
    text = " ".join(w.text for w in words if start_s <= w.start < end_s).strip()
    return text if len(text) <= cap else text[:cap].rsplit(" ", 1)[0] + " …"


def judge_weight(weights: Mapping[str, float], signals_used: Sequence[str]) -> float:
    """Share of the final score the judge gets: its weight relative to the raw weights of
    the signals that were actually available (mirrors the signal re-normalisation)."""
    w = float(weights.get("llm_moment_score", 0.0))
    if w <= 0:
        return 0.0
    rest = sum(float(weights.get(s, 0.0)) for s in signals_used)
    return w / (w + rest) if rest > 0 else 1.0


def blend(signal_score: float, overall: float, w: float) -> float:
    return round((1.0 - w) * signal_score + w * overall, 4)


def verdict_json(v: JudgeVerdict, model: str, prompt_id: str) -> dict[str, Any]:
    return {
        "overall": round(v.overall, 4),
        "scores": {k: round(float(x), 4) for k, x in v.scores.items()},
        "setup_payoff": v.setup_payoff,
        "titles": list(v.titles),
        "hook": v.hook,
        "reason": v.reason,
        "model": model,
        "prompt_id": prompt_id,
    }


class Judging:
    def __init__(
        self, judge: LLMJudge, blobs: BlobStore, workspace_id: WorkspaceId, cfg: JudgeConfig
    ) -> None:
        self.judge = judge
        self.blobs = blobs
        self.workspace_id = workspace_id
        self.cfg = cfg

    # ---------------------------------------------------------------- selection
    def budget(self, duration_s: float, params: Mapping[str, Any]) -> int:
        per_hour = float(params.get("judge_top_n_per_hour", 30))
        n = math.ceil(max(duration_s, 1.0) / 3600.0 * per_hour)
        return max(1, min(n, self.cfg.max_windows_per_video))

    # ---------------------------------------------------------------- cache
    def _cache_key(self, item: JudgeInput) -> str:
        payload = json.dumps(
            {
                "t": item.transcript,
                "d": round(item.end_s - item.start_s, 1),
                "h": {k: round(v, 2) for k, v in sorted(item.hints.items())},
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        return sha256_text(self.judge.prompt_id, self.judge.model, payload)

    def _blob(self, key: str) -> BlobKey:
        return BlobKey.for_workspace(self.workspace_id, "judge-cache", key[:2], f"{key}.json")

    def _cache_get(self, key: str, index: int) -> JudgeVerdict | None:
        blob = self._blob(key)
        if not self.blobs.exists(blob):
            return None
        try:
            with self.blobs.open_local(blob) as p:
                d = json.loads(p.read_text(encoding="utf-8"))
            return JudgeVerdict(
                index,
                d["scores"],
                d["setup_payoff"],
                d["overall"],
                tuple(d["titles"]),
                d["hook"],
                d["reason"],
            )
        except (OSError, KeyError, ValueError):
            return None  # corrupt cache entry: just re-judge

    def _cache_put(self, key: str, v: JudgeVerdict) -> None:
        with tempfile.TemporaryDirectory(prefix="clipforge-judge-") as tmp:
            path = Path(tmp) / "v.json"
            path.write_text(
                json.dumps(verdict_json(v, self.judge.model, self.judge.prompt_id)),
                encoding="utf-8",
            )
            self.blobs.put(self._blob(key), path)

    # ---------------------------------------------------------------- run
    def run(self, targets: Sequence[JudgeTarget], words: Sequence[Word]) -> JudgeOutcome:
        out = JudgeOutcome(requested=len(targets))
        items: list[tuple[JudgeTarget, JudgeInput, str]] = []
        for i, t in enumerate(targets):
            hints = {
                k: float(v)
                for k, v in t.components.items()
                if isinstance(v, int | float) and k not in ("peak_s", "signal_score")
            }
            item = JudgeInput(
                i,
                t.start_s,
                t.end_s,
                window_transcript(words, t.start_s, t.end_s, self.cfg.max_transcript_chars),
                hints,
            )
            key = self._cache_key(item)
            cached = self._cache_get(key, i)
            if cached is not None:
                out.verdicts[t.key] = cached
                out.from_cache += 1
            else:
                items.append((t, item, key))

        for start in range(0, len(items), self.cfg.batch_size):
            batch = items[start : start + self.cfg.batch_size]
            try:
                res = self.judge.judge([it for _, it, _ in batch])
            except JudgeUnavailable as exc:
                out.failed += len(batch)
                out.errors.append(str(exc)[:200])
                log.warning("judge_batch_failed", size=len(batch), error=str(exc)[:200])
                continue
            out.usage = out.usage + res.usage
            by_index = {v.index: v for v in res.verdicts}
            for t, it, key in batch:
                v = by_index.get(it.index)
                if v is None:
                    out.failed += 1
                    continue
                out.verdicts[t.key] = v
                self._cache_put(key, v)
        return out


def categories_line(v: JudgeVerdict) -> str:
    top = sorted(((c, v.scores.get(c, 0.0)) for c in CATEGORIES), key=lambda kv: -kv[1])[:2]
    return ", ".join(f"{c} {s:.1f}" for c, s in top if s >= 0.4)
