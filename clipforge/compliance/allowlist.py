"""Source allowlist enforcement (spec §2). Pure function, so it's trivial to test.

Called by the IngestSource service (M1) before anything is downloaded. Every decision is
audit-logged by the caller; an override is only honoured when ``override=True`` was passed
explicitly (CLI ``--override`` / API ``override`` flag).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from clipforge.domain.values import ClipPolicy, SourceStatus


class Verdict(StrEnum):
    ALLOW = "allow"
    ALLOW_WITH_OVERRIDE = "allow_with_override"  # processed, but flagged + audit-logged
    REFUSE = "refuse"


@dataclass(frozen=True, slots=True)
class SourceFacts:
    """What we know about the channel behind a URL. ``None`` = channel not in the allowlist."""

    clip_policy: ClipPolicy
    status: SourceStatus
    has_credit_text: bool


@dataclass(frozen=True, slots=True)
class AllowlistDecision:
    verdict: Verdict
    reason: str  # shown to the user and stored in audit_log


def decide(source: SourceFacts | None, *, override: bool) -> AllowlistDecision:
    """Decide whether a video from this source may be processed.

    Policy (ADR-0010):
      - Retired source → REFUSE, even with override. A takedown is a promise to the creator.
      - Not in allowlist, or policy UNKNOWN → REFUSE, unless override → ALLOW_WITH_OVERRIDE.
      - campaign / written_permission without credit text → REFUSE unless override. Those
        policies almost always require credit, so a missing credit is a setup mistake.
      - Otherwise → ALLOW.
    """
    if source is not None and source.status is SourceStatus.RETIRED:
        return AllowlistDecision(Verdict.REFUSE, "source is retired (takedown); override ignored")

    if source is None or source.clip_policy is ClipPolicy.UNKNOWN:
        why = "channel not in allowlist" if source is None else "channel clip policy is unknown"
        if override:
            return AllowlistDecision(Verdict.ALLOW_WITH_OVERRIDE, f"{why}; processed via override")
        return AllowlistDecision(Verdict.REFUSE, f"{why}; add it to sources or pass --override")

    if source.clip_policy in _CREDIT_REQUIRED and not source.has_credit_text:
        why = f"{source.clip_policy.value} source has no required_credit_text"
        if override:
            return AllowlistDecision(Verdict.ALLOW_WITH_OVERRIDE, f"{why}; processed via override")
        return AllowlistDecision(Verdict.REFUSE, f"{why}; set it on the source first")

    return AllowlistDecision(Verdict.ALLOW, f"allowed by {source.clip_policy.value} policy")


_CREDIT_REQUIRED = frozenset({ClipPolicy.CAMPAIGN, ClipPolicy.WRITTEN_PERMISSION})
