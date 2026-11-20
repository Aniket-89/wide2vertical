"""Synchronous in-process EventBus. Swappable for Redis Streams later."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from typing import Any

import structlog

from clipforge.domain.events import DomainEvent

log = structlog.get_logger(__name__)


class InProcessEventBus:
    def __init__(self) -> None:
        self._handlers: dict[type[DomainEvent], list[Callable[[Any], None]]] = defaultdict(list)

    def subscribe(self, event_type: type[Any], handler: Callable[[Any], None]) -> None:
        self._handlers[event_type].append(handler)

    def publish(self, event: DomainEvent) -> None:
        for etype, handlers in self._handlers.items():
            if isinstance(event, etype):
                for h in handlers:
                    try:
                        h(event)
                    except Exception:  # a bad subscriber must not break the publisher
                        log.exception("event_handler_failed", event_type=type(event).__name__)
