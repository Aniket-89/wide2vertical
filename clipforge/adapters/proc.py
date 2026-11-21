"""Safe subprocess runner: argument lists only (never a shell string), a hard timeout,
and bounded captured output so a chatty tool can't exhaust memory or flood logs.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

MAX_OUTPUT_BYTES = 8 * 1024 * 1024  # stdout cap (yt-dlp -J on a long VOD can be a few MB)
MAX_ERR_CHARS = 4000


class ProcError(RuntimeError):
    def __init__(self, args: Sequence[str], returncode: int | None, stderr: str) -> None:
        tool = args[2] if len(args) > 2 and args[1] == "-m" else args[0]
        what = "timed out" if returncode is None else f"exited {returncode}"
        super().__init__(f"{tool} {what}: {stderr[-MAX_ERR_CHARS:]}")
        self.returncode = returncode
        self.stderr = stderr


@dataclass(frozen=True, slots=True)
class ProcResult:
    stdout: str
    stderr: str


def run(args: Sequence[str], *, timeout_s: float, cwd: Path | None = None) -> ProcResult:
    if isinstance(args, str):  # defensive: a str is a Sequence[str] too
        raise TypeError("pass an argument list, not a string")
    try:
        cp = subprocess.run(  # noqa: S603 - args is a list, shell=False
            list(args), capture_output=True, timeout=timeout_s, check=False, shell=False, cwd=cwd
        )
    except subprocess.TimeoutExpired as exc:
        err = (exc.stderr or b"")[-MAX_ERR_CHARS:].decode("utf-8", "replace")
        raise ProcError(args, None, err) from exc
    stderr = cp.stderr[-MAX_ERR_CHARS:].decode("utf-8", "replace")
    if cp.returncode != 0:
        raise ProcError(args, cp.returncode, stderr)
    if len(cp.stdout) > MAX_OUTPUT_BYTES:
        raise ProcError(args, cp.returncode, f"stdout exceeded {MAX_OUTPUT_BYTES} bytes")
    return ProcResult(cp.stdout.decode("utf-8", "replace"), stderr)


def safe_url_arg(url: str) -> str:
    """Validate a URL that goes on a command line after ``--``. Rejects anything that
    could be read as an option, plus non-http(s) schemes (file://, etc.)."""
    u = url.strip()
    if u.startswith("-") or not u.lower().startswith(("https://", "http://")) or "\x00" in u:
        raise ValueError(f"refusing unsafe URL argument: {url[:80]!r}")
    return u
