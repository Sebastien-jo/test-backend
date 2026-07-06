"""Unit tests for the local file storage (infra-free, tmp filesystem)."""

from pathlib import Path

import pytest

from app.services.storage import LocalFileStorage, sanitize_filename


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("report.pdf", "report.pdf"),
        ("../../etc/passwd", "passwd"),
        ("../../../x", "x"),
        ("nested/dir/file.txt", "file.txt"),
        ("..", "unnamed"),
        ("", "unnamed"),
        ("with\x00null.pdf", "withnull.pdf"),
        ("  spaced.pdf  ", "spaced.pdf"),
    ],
)
def test_sanitize_filename(raw: str, expected: str) -> None:
    assert sanitize_filename(raw) == expected


async def test_local_storage_roundtrip(tmp_path: Path) -> None:
    storage = LocalFileStorage(tmp_path)
    key = "org/doc/file.txt"

    await storage.save(key, b"hello")
    assert (tmp_path / key).read_bytes() == b"hello"

    await storage.delete(key)
    assert not (tmp_path / key).exists()


@pytest.mark.parametrize("key", ["../escape.txt", "../../etc/passwd", "sub/../../out.txt"])
async def test_local_storage_rejects_path_traversal(tmp_path: Path, key: str) -> None:
    storage = LocalFileStorage(tmp_path)
    with pytest.raises(ValueError):
        await storage.save(key, b"x")
