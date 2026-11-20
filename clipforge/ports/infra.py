"""Infrastructure ports. Core code depends on these, never on concrete adapters."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from enum import IntEnum
from pathlib import Path
from typing import Any, Protocol, TypeVar
from uuid import UUID

from clipforge.domain.events import DomainEvent
from clipforge.domain.values import BlobKey, WorkspaceId

E = TypeVar("E", bound=DomainEvent)


class Clock(Protocol):
    def now(self) -> datetime: ...


class BlobStore(Protocol):
    def put(self, key: BlobKey, src: Path) -> int:
        """Copy ``src`` into the store under ``key``. Returns bytes written."""
        ...

    def open_local(self, key: BlobKey) -> AbstractContextManager[Path]:
        """Yield a local filesystem path for reading (downloads first for remote stores)."""
        ...

    def exists(self, key: BlobKey) -> bool: ...

    def delete(self, key: BlobKey) -> None: ...

    def iter_keys(self, prefix: BlobKey) -> Iterator[BlobKey]: ...


class Priority(IntEnum):
    HIGH = 0
    NORMAL = 5
    LOW = 9


class JobQueue(Protocol):
    def enqueue(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        priority: Priority = Priority.NORMAL,
        delay_s: int = 0,
    ) -> UUID: ...


class EventBus(Protocol):
    def publish(self, event: DomainEvent) -> None: ...

    def subscribe(self, event_type: type[E], handler: Callable[[E], None]) -> None: ...


@dataclass(frozen=True, slots=True)
class QuotaDecision:
    allowed: bool
    reason: str = ""


class QuotaService(Protocol):
    """SaaS seam. The MVP adapter always allows."""

    def check(self, workspace_id: WorkspaceId, kind: str, quantity: float) -> QuotaDecision: ...
