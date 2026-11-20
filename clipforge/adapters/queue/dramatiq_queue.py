"""JobQueue backed by Dramatiq + Redis.

Every job goes through one generic actor (``clipforge_dispatch``) that looks up a
handler by ``kind``. That keeps Dramatiq out of core code: handlers are plain
functions registered in ``apps.worker``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

import dramatiq
from dramatiq.brokers.redis import RedisBroker

from clipforge.domain.ids import new_id
from clipforge.ports.infra import Priority

DISPATCH_ACTOR = "clipforge_dispatch"


_PRIORITY_SUFFIX = {Priority.HIGH: "_high", Priority.NORMAL: "", Priority.LOW: "_low"}


def queue_name(base: str, priority: Priority) -> str:
    """Redis has no per-message priority, so each priority level is its own queue."""
    return base + _PRIORITY_SUFFIX[priority]


def all_queue_names(base: str) -> list[str]:
    return [queue_name(base, p) for p in Priority]


def make_broker(
    redis_url: str, *, install: bool = False, queue_bases: tuple[str, ...] = ()
) -> RedisBroker:
    """Create a Redis broker.

    ``install=True`` (worker processes only) makes it Dramatiq's global broker, so actors bind
    to it. Producers (API/CLI) must not install, because replacing the global broker inside a
    running worker breaks it. ``queue_bases`` declares every priority variant of each base
    queue: a Dramatiq worker only consumes *declared* queues, and the dispatch actor itself
    only declares ``default``.
    """
    broker = RedisBroker(url=redis_url)  # type: ignore[no-untyped-call]
    for base in queue_bases:
        for name in all_queue_names(base):
            broker.declare_queue(name)
    if install:
        dramatiq.set_broker(broker)
    return broker


class DramatiqJobQueue:
    def __init__(self, broker: dramatiq.Broker, queue_for_kind: Mapping[str, str]) -> None:
        self._broker = broker
        self._queue_for_kind = dict(queue_for_kind)

    def enqueue(
        self,
        kind: str,
        payload: Mapping[str, Any],
        *,
        priority: Priority = Priority.NORMAL,
        delay_s: int = 0,
    ) -> UUID:
        job_id = new_id()
        message: dramatiq.Message[Any] = dramatiq.Message(
            queue_name=queue_name(self._queue_for_kind.get(kind, "default"), priority),
            actor_name=DISPATCH_ACTOR,
            args=(kind, str(job_id), dict(payload)),
            kwargs={},
            options={},
        )
        self._broker.enqueue(message, delay=delay_s * 1000 if delay_s else None)
        return job_id
