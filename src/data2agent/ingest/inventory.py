"""Deterministic file inventory.

The walk is sorted, read-only, and independent of filesystem iteration order, so
the same dataset always yields the same inventory in the same sequence. Symlinks
are recorded but never followed: following them would let the "immutable dataset"
extend past its own boundary.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..errors import SourceError
from . import formats
from .checksum import hash_file

# Host and tooling detritus that is not part of the scientific record. Anything
# skipped is reported, so the inventory is never silently incomplete.
DEFAULT_EXCLUDES = frozenset(
    {".git", ".svn", ".hg", "__pycache__", ".DS_Store", ".ipynb_checkpoints"}
)


@dataclass
class FileEntry:
    """One file, as bytes on disk."""

    path: str  # POSIX-style, relative to the dataset root
    size: int
    sha256: str
    format: formats.FormatInfo
    is_symlink: bool = False

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "path": self.path,
            "size": self.size,
            "sha256": self.sha256,
            **self.format.as_dict(),
        }
        if self.is_symlink:
            payload["is_symlink"] = True
        return payload


@dataclass
class Inventory:
    root: Path
    files: list[FileEntry]
    skipped: list[str]
    warnings: list[str]

    @property
    def total_bytes(self) -> int:
        return sum(entry.size for entry in self.files)


def build(root: Path, excludes: frozenset[str] = DEFAULT_EXCLUDES) -> Inventory:
    """Walk ``root`` and checksum every file beneath it."""
    root = root.expanduser()
    if not root.exists():
        raise SourceError(f"dataset source does not exist: {root}")
    if not root.is_dir():
        raise SourceError(f"dataset source is not a directory: {root}")

    entries: list[FileEntry] = []
    skipped: list[str] = []
    warnings: list[str] = []

    for path in _walk(root, excludes, skipped):
        relative = path.relative_to(root).as_posix()
        try:
            stat = path.lstat()
            digest = hash_file(path)
        except OSError as error:
            warnings.append(f"could not read '{relative}': {error}")
            continue
        detected, notes = formats.detect(path)
        warnings.extend(f"{relative}: {note}" for note in notes)
        entries.append(
            FileEntry(
                path=relative,
                size=stat.st_size if not path.is_symlink() else path.stat().st_size,
                sha256=digest,
                format=detected,
                is_symlink=path.is_symlink(),
            )
        )

    entries.sort(key=lambda entry: entry.path)
    if not entries:
        warnings.append("no files found under the dataset source")
    return Inventory(root=root, files=entries, skipped=sorted(skipped), warnings=warnings)


def _walk(root: Path, excludes: frozenset[str], skipped: list[str]) -> list[Path]:
    """Collect files in sorted order, recording anything deliberately skipped."""
    found: list[Path] = []
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            children = sorted(current.iterdir(), key=lambda child: child.name)
        except OSError as error:
            skipped.append(f"{current.relative_to(root).as_posix() or '.'} ({error})")
            continue
        for child in children:
            if child.name in excludes:
                skipped.append(child.relative_to(root).as_posix())
                continue
            if child.is_dir() and not child.is_symlink():
                stack.append(child)
            elif child.is_file():
                found.append(child)
            elif child.is_symlink():
                # A symlink to a directory is a boundary we refuse to cross.
                skipped.append(f"{child.relative_to(root).as_posix()} (symlink not followed)")
            else:
                skipped.append(f"{child.relative_to(root).as_posix()} (not a regular file)")
    return sorted(found, key=lambda path: path.as_posix())
