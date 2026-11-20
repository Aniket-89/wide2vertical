"""Shared chat-adapter helpers: author hashing and HTTP with timeouts + backoff."""

from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from typing import Any

from clipforge.ports.analysis import ChatUnavailable


def hash_author(salt: str, name: str) -> str:
    """Chat authors are third parties: store a salted hash, never the username (ADR-0022).
    Enough to count unique chatters; not reversible to a person."""
    return hashlib.sha256(f"{salt}\x1f{name.lower()}".encode()).hexdigest()[:16]


def post_json(
    url: str,
    body: Any,
    headers: dict[str, str],
    *,
    timeout_s: float = 15,
    retries: int = 3,
    backoff_s: float = 1.5,
) -> Any:
    data = json.dumps(body).encode()
    last: Exception | None = None
    for attempt in range(retries):
        req = urllib.request.Request(  # noqa: S310 - fixed https URL from the adapter
            url, data=data, headers={"Content-Type": "application/json", **headers}
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:  # noqa: S310
                raw = resp.read(32 * 1024 * 1024)  # bounded
            return json.loads(raw)
        except urllib.error.HTTPError as exc:
            last = exc
            if exc.code in (400, 401, 403, 404):  # not transient: don't hammer
                break
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            last = exc
        time.sleep(backoff_s * (2**attempt))
    raise ChatUnavailable(f"chat request failed: {type(last).__name__}: {last}")
