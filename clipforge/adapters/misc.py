"""Small default adapters: system clock, allow-all quota."""

from __future__ import annotations

from datetime import UTC, datetime

from clipforge.domain.values import WorkspaceId
from clipforge.ports.infra import QuotaDecision


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class AllowAllQuota:
    def check(self, workspace_id: WorkspaceId, kind: str, quantity: float) -> QuotaDecision:
        return QuotaDecision(allowed=True)
