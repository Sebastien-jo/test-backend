"""File storage abstraction.

Callers speak only in opaque logical *keys* (never filesystem paths), so the
local backend can be swapped for S3 without touching services or endpoints.
"""

import unicodedata
from enum import StrEnum
from pathlib import Path
from typing import Protocol

import anyio

from app.core.config import settings


class FileStorage(Protocol):
    """Storage contract. Keys are opaque strings owned by the caller."""

    async def save(self, key: str, content: bytes) -> None: ...

    async def open(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None: ...


class StorageBackend(StrEnum):
    LOCAL = "local"
    S3 = "s3"


def sanitize_filename(filename: str) -> str:
    """Reduce an untrusted filename to a safe basename.

    Strips any directory components (so `../../etc/passwd` -> `passwd`) and
    control/non-printable characters. Falls back to `unnamed` if nothing is left.
    The *original* name is kept in the DB for display; this is only for the key.
    """
    # Path().name drops directory parts, including `..` traversal segments.
    name = Path(filename).name
    name = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C").strip()
    if name in {"", ".", ".."}:
        return "unnamed"
    return name


class LocalFileStorage:
    """Stores blobs on the local filesystem under a fixed root directory."""

    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    def _resolve(self, key: str) -> Path:
        # Defense in depth: even with sanitized keys, resolve the final path and
        # confirm it stays under the root, rejecting any traversal attempt.
        target = (self._root / key).resolve()
        if target != self._root and self._root not in target.parents:
            raise ValueError(f"Storage key escapes the root directory: {key!r}")
        return target

    async def save(self, key: str, content: bytes) -> None:
        path = self._resolve(key)
        await anyio.to_thread.run_sync(self._write_sync, path, content)

    async def open(self, key: str) -> bytes:
        path = self._resolve(key)
        return await anyio.to_thread.run_sync(path.read_bytes)

    async def delete(self, key: str) -> None:
        path = self._resolve(key)
        await anyio.to_thread.run_sync(self._delete_sync, path)

    @staticmethod
    def _write_sync(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    @staticmethod
    def _delete_sync(path: Path) -> None:
        path.unlink(missing_ok=True)


def get_storage() -> FileStorage:
    """FastAPI dependency: pick the storage backend from settings.

    Endpoints and services only ever see the `FileStorage` Protocol.
    """
    if settings.storage_backend == StorageBackend.LOCAL:
        return LocalFileStorage(Path(settings.storage_path))
    # An S3 backend (presigned upload URLs, streaming) would slot in here without
    # changing any caller — that is the point of the Protocol.
    raise NotImplementedError(f"Unsupported storage backend: {settings.storage_backend!r}")
