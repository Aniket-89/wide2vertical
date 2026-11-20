"""Test fakes for infrastructure ports."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from clipforge.domain.ids import new_id
from clipforge.ports.infra import Priority


class FakeClock:
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 1, 1, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


@dataclass
class EnqueuedJob:
    id: UUID
    kind: str
    payload: Mapping[str, Any]
    priority: Priority
    delay_s: int


@dataclass
class FakeJobQueue:
    jobs: list[EnqueuedJob] = field(default_factory=list)

    def enqueue(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        priority: Priority = Priority.NORMAL,
        delay_s: int = 0,
    ) -> UUID:
        job = EnqueuedJob(new_id(), kind, dict(payload), priority, delay_s)
        self.jobs.append(job)
        return job.id
