"""M2 use case: transcript (+ chat, + audio) → signals → ranked candidate windows.

Graceful degradation is the rule: chat or audio being unavailable, or one optional signal
crashing, never fails the video. Only the required transcript signal can.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import math
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from uuid import UUID

import numpy as np
import structlog

from clipforge.domain.values import BlobKey, Platform, Stage
from clipforge.ports.analysis import (
    AnalysisPipelineStore,
    AudioAnalyzer,
    CandidateWindow,
    ChatMessage,
    ChatProvider,
    ChatUnavailable,
    FloatArray,
    ScoringProfileRecord,
    SignalContext,
    SignalExtractor,
    SignalSeries,
)
from clipforge.ports.infra import BlobStore, EventBus
from clipforge.ports.judge import JudgeUnavailable, LLMJudge
from clipforge.ports.pipeline import AssetRecord, PipelineError, ResolvedVideo, VideoRecord, Word
from clipforge.scoring.generate import generate_candidates, summarize
from clipforge.services._common import require_not_retired, require_video, sha256_text, track_stage
from clipforge.services.judging import (
    JudgeConfig,
    JudgeOutcome,
    JudgeTarget,
    Judging,
    blend,
    categories_line,
    judge_weight,
    verdict_json,
)

log = structlog.get_logger(__name__)

AUDIO_RMS_SERIES = "_audio_rms_db"  # cached raw loudness; not a scoring signal itself


@dataclass(frozen=True)
class AnalyzeResult:
    video_id: UUID
    profile: ScoringProfileRecord
    candidates: list[CandidateWindow]
    weights_used: dict[str, float]
    signals: dict[str, str]  # name -> "computed" | "cached" | "unavailable" | "failed: ..."
    chat_status: str
    combined: dict[str, float] = field(default_factory=dict)
    judge_status: str = "disabled"
    judge_cost_usd: float = 0.0


def _hash_params(params: Mapping[str, Any]) -> str:
    return sha256_text(json.dumps(params, sort_keys=True, default=str))


class AnalyzePipeline:
    def __init__(
        self,
        store: AnalysisPipelineStore,
        signals: Sequence[SignalExtractor],
        chat_providers: Mapping[Platform, ChatProvider],
        audio: AudioAnalyzer,
        blobs: BlobStore,
        events: EventBus,
        judge: LLMJudge | None = None,
        judge_config: JudgeConfig | None = None,
    ) -> None:
        self.judge = judge
        self.judge_config = judge_config or JudgeConfig()
        self.store = store
        self.signals = signals
        self.chat_providers = chat_providers
        self.audio = audio
        self.blobs = blobs
        self.events = events

    # ------------------------------------------------------------ inputs
    def _chat(self, video: VideoRecord) -> tuple[list[ChatMessage] | None, str, str]:
        """(messages or None, status, cache key). Cached as a chat_dump asset."""
        cached = self.store.find_asset(video.id, "chat_dump")
        if cached is not None and self.blobs.exists(cached.blob_key):
            with (
                self.blobs.open_local(cached.blob_key) as p,
                gzip.open(p, "rt", encoding="utf-8") as f,
            ):
                msgs = [
                    ChatMessage(m["t"], m["a"], m["x"], tuple(m.get("e", ())))
                    for m in map(json.loads, f)
                ]
            return msgs, f"cached ({len(msgs)} messages)", cached.content_hash
        provider = self.chat_providers.get(video.platform)
        if provider is None:
            return None, "disabled (no chat provider enabled for this platform)", "none"
        rv = ResolvedVideo(
            video.platform,
            video.url,
            video.external_id,
            video.channel_handle or "",
            video.channel_id,
            "",
            video.duration_s,
        )
        try:
            msgs = sorted(provider.fetch_replay(rv), key=lambda m: m.t_s)
        except ChatUnavailable as exc:
            log.warning("chat_unavailable", video_id=str(video.id), error=str(exc)[:300])
            return None, f"unavailable: {str(exc)[:200]}", "none"
        except Exception as exc:  # an adapter bug must not fail the video either
            log.exception("chat_provider_crashed", video_id=str(video.id))
            return None, f"unavailable: {type(exc).__name__}", "none"
        key = self._save_chat(video.id, msgs)
        return msgs, f"fetched ({len(msgs)} messages)", key

    def _save_chat(self, video_id: UUID, msgs: list[ChatMessage]) -> str:
        with tempfile.TemporaryDirectory(prefix="clipforge-chat-") as tmp:
            path = Path(tmp) / "chat.jsonl.gz"
            with gzip.open(path, "wt", encoding="utf-8") as f:
                for m in msgs:
                    row = {"t": round(m.t_s, 3), "a": m.author, "x": m.text}
                    if m.emotes:
                        row["e"] = list(m.emotes)
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            key = BlobKey.for_workspace(
                self.store.workspace_id, "videos", str(video_id), "chat.jsonl.gz"
            )
            written = self.blobs.put(key, path)
        self.store.save_asset(video_id, "chat_dump", AssetRecord(key, digest, written))
        return digest

    def _audio_rms(self, video: VideoRecord) -> tuple[FloatArray | None, str]:
        asset = self.store.find_asset(video.id, "audio")
        if asset is None:
            return None, "none"
        input_hash = sha256_text(asset.content_hash, self.audio.version)
        cached = self.store.find_signal(video.id, AUDIO_RMS_SERIES, input_hash)
        if cached is not None:
            return cached.values, input_hash
        if not self.blobs.exists(asset.blob_key):  # raw audio removed by retention
            return None, "none"
        try:
            with self.blobs.open_local(asset.blob_key) as path:
                rms = self.audio.rms_db_per_second(path)
        except PipelineError as exc:
            log.warning("audio_analysis_failed", video_id=str(video.id), error=str(exc)[:300])
            return None, "none"
        self.store.save_signal(
            video.id, SignalSeries(AUDIO_RMS_SERIES, self.audio.version, rms), input_hash
        )
        return rms, input_hash

    # ------------------------------------------------------------ signals
    def _signals(
        self,
        video: VideoRecord,
        words: Sequence[Word],
        transcript_key: str,
        profile: ScoringProfileRecord,
        n: int,
    ) -> tuple[dict[str, SignalSeries], dict[str, str], str]:
        chat, chat_status, chat_key = self._chat(video)
        rms, audio_key = self._audio_rms(video)
        ctx = SignalContext(
            n_seconds=n, words=words, chat=chat, audio_rms_db=rms, params=profile.params
        )
        params_key = _hash_params(profile.params)
        out: dict[str, SignalSeries] = {}
        status: dict[str, str] = {}
        for sig in self.signals:
            input_hash = sha256_text(
                sig.name, sig.version, transcript_key, chat_key, audio_key, params_key, str(n)
            )
            cached = self.store.find_signal(video.id, sig.name, input_hash)
            if cached is not None:
                out[sig.name], status[sig.name] = cached, "cached"
                continue
            try:
                series = sig.extract(ctx)
            except Exception as exc:
                if sig.required:
                    raise
                log.exception("signal_failed", signal=sig.name, video_id=str(video.id))
                status[sig.name] = f"failed: {type(exc).__name__}"
                continue
            if series is None:
                status[sig.name] = "unavailable"
                continue
            values = np.nan_to_num(np.clip(series.values, 0.0, 1.0))
            series = SignalSeries(series.name, series.version, values, series.meta)
            self.store.save_signal(video.id, series, input_hash)
            out[sig.name], status[sig.name] = series, "computed"
        return out, status, chat_status

    # ------------------------------------------------------------ use case
    def analyze(
        self,
        video_id: UUID,
        profile_ref: str | UUID | None = None,
        *,
        persist: bool = True,
        weight_overrides: Mapping[str, float] | None = None,
        param_overrides: Mapping[str, Any] | None = None,
        with_judge: bool | None = None,
    ) -> AnalyzeResult:
        """Compute signals (cached), candidates, and (if enabled) LLM-judge them.

        ``persist=False`` is the replay-harness mode: same computation, nothing written to
        candidates. The judge runs there only with ``with_judge=True``, because it costs money.
        """
        if with_judge is None:
            with_judge = persist
        video = require_video(self.store, video_id)
        require_not_retired(self.store, video, Stage.SIGNALS)
        t = self.store.get_transcript(video.id)
        if t is None:
            raise PipelineError("no transcript yet; run the transcribe stage first")
        transcript_key, words = t
        base = self.store.get_scoring_profile(profile_ref)
        profile = ScoringProfileRecord(
            base.id,
            base.name,
            base.version,
            {**base.weights, **(weight_overrides or {})},
            {**base.params, **(param_overrides or {})},
        )
        last_word = max((w.end for w in words), default=0.0)
        duration = float(video.duration_s or last_word or 1.0)
        n = max(1, math.ceil(duration))

        if persist:
            with track_stage(
                self.store,
                self.events,
                video.id,
                Stage.SIGNALS,
                sha256_text(transcript_key, _hash_params(profile.params)),
            ):
                signals, status, chat_status = self._signals(
                    video, words, transcript_key, profile, n
                )
        else:
            signals, status, chat_status = self._signals(video, words, transcript_key, profile, n)

        def candidates() -> tuple[list[CandidateWindow], FloatArray, dict[str, float]]:
            return generate_candidates(signals, words, duration, profile.weights, profile.params)

        if persist:
            with track_stage(
                self.store,
                self.events,
                video.id,
                Stage.CANDIDATES,
                sha256_text(*sorted(status), _hash_params(profile.weights)),
            ):
                ranked, combined, used = candidates()
                self.store.replace_candidates(video.id, profile.id, ranked)
        else:
            ranked, combined, used = candidates()

        judge_status, cost = "disabled", 0.0
        if with_judge and self.judge is not None and self.judge.enabled and ranked:
            w = judge_weight(profile.weights, list(used))
            if persist:
                outcome = self._judge_stored(video, profile, words, duration, w)
                stored = self.store.list_candidates(video.id, profile.id, limit=1000)
                scores = {(c.start_s, c.end_s): c.score for c in stored}
                ranked = sorted(
                    (replace(c, score=scores.get((c.start_s, c.end_s), c.score)) for c in ranked),
                    key=lambda c: -c.score,
                )
            else:
                outcome, ranked = self._judge_in_memory(ranked, words, duration, profile, w)
            judge_status, cost = outcome.status, self.judge_config.cost_usd(outcome.usage)
        elif with_judge and self.judge is not None:
            judge_status = "disabled (NullJudge)"
        return AnalyzeResult(
            video.id,
            profile,
            ranked,
            used,
            status,
            chat_status,
            summarize(combined),
            judge_status,
            round(cost, 6),
        )

    # ------------------------------------------------------------ judge (M3)
    def _judging(self, judge: LLMJudge) -> Judging:
        return Judging(judge, self.blobs, self.store.workspace_id, self.judge_config)

    def _record_usage(self, judge: LLMJudge, video_id: UUID, outcome: JudgeOutcome) -> None:
        u = outcome.usage
        if not (u.input_tokens or u.output_tokens):
            return
        meta = {"model": judge.model, "prompt_id": judge.prompt_id}
        self.store.record_usage(
            "llm_tokens_in",
            u.input_tokens,
            "tokens",
            video_id,
            cache_read=u.cache_read_tokens,
            cache_write=u.cache_write_tokens,
            **meta,
        )
        self.store.record_usage("llm_tokens_out", u.output_tokens, "tokens", video_id, **meta)
        self.store.record_usage(
            "llm_cost_usd_est", round(self.judge_config.cost_usd(u), 6), "usd", video_id, **meta
        )

    def _judge_stored(
        self,
        video: VideoRecord,
        profile: ScoringProfileRecord,
        words: Sequence[Word],
        duration: float,
        w: float,
    ) -> JudgeOutcome:
        judge = self.judge
        assert judge is not None
        judging = self._judging(judge)
        pending = [
            c
            for c in self.store.list_candidates(video.id, profile.id, limit=1000)
            if c.status == "pending"
        ]
        top = pending[: judging.budget(duration, profile.params)]
        targets = [
            JudgeTarget(c.id, c.start_s, c.end_s, c.score, c.components, c.reason) for c in top
        ]
        outcome = JudgeOutcome()
        stage_hash = sha256_text(judge.prompt_id, judge.model, *(str(t.key) for t in targets))
        try:
            with track_stage(self.store, self.events, video.id, Stage.JUDGE, stage_hash):
                outcome = judging.run(targets, words)
                self._record_usage(judge, video.id, outcome)
                for t in targets:
                    v = outcome.verdicts.get(t.key)
                    if v is None:
                        continue
                    comps = {
                        **t.components,
                        "signal_score": t.signal_score,
                        "llm_moment_score": round(v.overall, 4),
                    }
                    cats = categories_line(v)
                    reason = v.reason + (f" ({cats})" if cats else "") + f" · signals: {t.reason}"
                    self.store.apply_judgement(
                        t.key,
                        score=blend(t.signal_score, v.overall, w),
                        components=comps,
                        judge={
                            **verdict_json(v, judge.model, judge.prompt_id),
                            "weight": round(w, 4),
                        },
                        titles=v.titles,
                        hook=v.hook,
                        reason=reason,
                    )
                self.store.rerank(video.id, profile.id)
                if targets and not outcome.verdicts:
                    raise JudgeUnavailable(outcome.status)
        except JudgeUnavailable:
            pass  # recorded as a failed judge stage; candidates keep their signal scores
        return outcome

    def _judge_in_memory(
        self,
        ranked: list[CandidateWindow],
        words: Sequence[Word],
        duration: float,
        profile: ScoringProfileRecord,
        w: float,
    ) -> tuple[JudgeOutcome, list[CandidateWindow]]:
        judge = self.judge
        assert judge is not None
        judging = self._judging(judge)
        n = judging.budget(duration, profile.params)
        targets = [
            JudgeTarget(i, c.start_s, c.end_s, c.score, c.components, c.reason)
            for i, c in enumerate(ranked[:n])
        ]
        outcome = judging.run(targets, words)
        rescored = [
            replace(c, score=blend(c.score, outcome.verdicts[i].overall, w))
            if i in outcome.verdicts
            else c
            for i, c in enumerate(ranked)
        ]
        return outcome, sorted(rescored, key=lambda c: -c.score)
