"""Time-ordered ids (UUIDv7). Stdlib uuid lacks uuid7 before Python 3.14."""

from __future__ import annotations

from uuid import UUID

import uuid_utils


def new_id() -> UUID:
    return UUID(str(uuid_utils.uuid7()))
