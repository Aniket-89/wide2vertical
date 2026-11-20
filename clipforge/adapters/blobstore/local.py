"""Local-disk BlobStore. Keys map 1:1 to paths under a root directory."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from clipforge.domain.values import BlobKey


class LocalBlobStore:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()
        self._root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: BlobKey) -> Path:
        p = (self._root / key.value).resolve()
        if not p.is_relative_to(self._root):  # defence in depth; BlobKey already validates
            raise ValueError(f"key escapes root: {key.value}")
        return p

    def put(self, key: BlobKey, src: Path) -> int:
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(dest.suffix + ".part")
        shutil.copyfile(src, tmp)
        tmp.replace(dest)  # atomic on the same filesystem
        return dest.stat().st_size

    @contextmanager
    def open_local(self, key: BlobKey) -> Iterator[Path]:
        p = self._path(key)
        if not p.exists():
            raise FileNotFoundError(key.value)
        yield p

    def exists(self, key: BlobKey) -> bool:
        return self._path(key).exists()

    def delete(self, key: BlobKey) -> None:
        self._path(key).unlink(missing_ok=True)

    def iter_keys(self, prefix: BlobKey) -> Iterator[BlobKey]:
        base = self._path(prefix)
        if base.is_file():
            yield prefix
            return
        if not base.exists():
            return
        for p in sorted(base.rglob("*")):
            if p.is_file() and not p.name.endswith(".part"):
                yield BlobKey(p.relative_to(self._root).as_posix())
