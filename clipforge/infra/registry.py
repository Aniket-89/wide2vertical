"""Composition root: the only place that imports concrete adapters.

Config picks adapters; everything else receives ports.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from clipforge.adapters.blobstore.local import LocalBlobStore
from clipforge.adapters.eventbus.in_process import InProcessEventBus
from clipforge.adapters.misc import AllowAllQuota, SystemClock
from clipforge.adapters.queue.dramatiq_queue import DISPATCH_ACTOR, make_broker
from clipforge.infra.config import Settings, get_settings
from clipforge.ports.infra import BlobStore, Clock, EventBus, QuotaService


@dataclass(frozen=True)
class Container:
    settings: Settings
    clock: Clock
    blobs: BlobStore
    events: EventBus
    quota: QuotaService


def build_container(settings: Settings) -> Container:
    return Container(
        settings=settings,
        clock=SystemClock(),
        blobs=LocalBlobStore(settings.blob_root),
        events=InProcessEventBus(),
        quota=AllowAllQuota(),
    )


@lru_cache
def get_container() -> Container:
    return build_container(get_settings())


def build_worker_broker(settings: Settings) -> Any:
    """Create and install the task broker for worker processes."""
    return make_broker(settings.redis_url)


__all__ = ["DISPATCH_ACTOR", "Container", "build_container", "build_worker_broker", "get_container"]
