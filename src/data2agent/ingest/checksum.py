"""Content hashing.

Every fact Data2Agent reports is ultimately anchored to a SHA-256 of the exact
bytes it was read from, so hashing is deliberately boring: one algorithm, one
streaming implementation, one canonical way to fold per-file digests into a
dataset identity.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from pathlib import Path

ALGORITHM = "sha256"
_CHUNK_BYTES = 1024 * 1024


def hash_file(path: Path) -> str:
    """Return the hex SHA-256 of a file, read in chunks and never held in memory."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK_BYTES):
            digest.update(chunk)
    return digest.hexdigest()


def hash_bytes(payload: bytes) -> str:
    """Return the hex SHA-256 of an in-memory payload."""
    return hashlib.sha256(payload).hexdigest()


def dataset_id(entries: Iterable[tuple[str, str]]) -> str:
    """Fold ``(relative_path, sha256)`` pairs into a single dataset identity.

    The pairs are sorted by path and serialised as ``path\\n sha256\\n`` so that
    the identity depends only on the dataset's content and layout -- not on walk
    order, filesystem, clock, or machine. Two ingests of the same bytes always
    produce the same ``dataset_id``; a single changed byte, a renamed file, or an
    added file always changes it.
    """
    digest = hashlib.sha256()
    for path, file_hash in sorted(entries, key=lambda item: item[0]):
        digest.update(path.encode("utf-8"))
        digest.update(b"\n")
        digest.update(file_hash.encode("ascii"))
        digest.update(b"\n")
    return f"{ALGORITHM}:{digest.hexdigest()}"
