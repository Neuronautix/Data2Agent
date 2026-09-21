"""Deterministic file inventory.

The walk is sorted, read-only, and independent of filesystem iteration order, so
the same dataset always yields the same inventory in the same sequence.

Symlinks are recorded but never followed, and the order of the checks below is
what makes that true: ``Path.is_file()`` follows a symlink and returns True for
a link to a regular file, so a symlink test placed after it never runs. The
effect is not cosmetic -- a link pointing outside the dataset would be hashed
and profiled, putting bytes from beyond the boundary into a manifest that claims
to describe an immutable dataset. So ``is_symlink()`` is tested first, always.
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

    def as_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "size": self.size,
            "sha256": self.sha256,
            **self.format.as_dict(),
        }


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
    symlinks: list[str] = []

    for path in walk(root, excludes, skipped, symlinks):
        relative = path.relative_to(root).as_posix()
        try:
            stat = path.lstat()
            digest = hash_file(path)
        except OSError as error:
            warnings.append(f"could not read '{relative}': {error}")
            continue
        detected, notes = formats.detect(path)
        warnings.extend(f"{relative}: {note}" for note in notes)
        entries.append(FileEntry(path=relative, size=stat.st_size, sha256=digest, format=detected))

    entries.sort(key=lambda entry: entry.path)
    if symlinks:
        # Loud, because a skipped symlink means the manifest describes less than
        # the directory contains -- and silence would read as "there were none".
        warnings.append(
            f"{len(symlinks)} symlink(s) were not followed and are absent from this "
            f"manifest: {sorted(symlinks)}"
        )
    if not entries:
        warnings.append("no files found under the dataset source")
    return Inventory(root=root, files=entries, skipped=sorted(skipped), warnings=warnings)


def walk(
    root: Path, excludes: frozenset[str], skipped: list[str], symlinks: list[str]
) -> list[Path]:
    """Collect regular files in sorted order, recording what was skipped and why.

    Public because the pipeline re-walks at the end of a run to prove the source
    did not gain or lose files while it was being read.
    """
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
            relative = child.relative_to(root).as_posix()
            # is_symlink() must come first: is_file() and is_dir() both follow
            # the link, so either would swallow the symlink before we see it.
            if child.is_symlink():
                symlinks.append(relative)
                skipped.append(f"{relative} (symlink not followed)")
            elif child.is_dir():
                stack.append(child)
            elif child.is_file():
                found.append(child)
            else:
                skipped.append(f"{relative} (not a regular file)")
    return sorted(found, key=lambda path: path.as_posix())
