"""Versioned LLM prompts. One file per prompt id; never edit a released version, add a new one.

docs/PROMPTS.md indexes every id with its content hash (a test keeps them in sync), and the id
is stored with every verdict so results are reproducible.
"""

from __future__ import annotations

import hashlib
from functools import cache
from importlib import resources

PROMPT_FILES = {"judge.moment.v1": "judge_moment_v1.md"}


@cache
def load(prompt_id: str) -> str:
    try:
        name = PROMPT_FILES[prompt_id]
    except KeyError:
        raise KeyError(f"unknown prompt id {prompt_id!r}") from None
    return resources.files(__package__).joinpath(name).read_text(encoding="utf-8").strip()


def fingerprint(prompt_id: str) -> str:
    """Short content hash: changes if the prompt text changes."""
    return hashlib.sha256(load(prompt_id).encode()).hexdigest()[:12]
