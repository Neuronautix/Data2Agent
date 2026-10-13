"""Which Data2Agent build is running (#33).

The package version alone cannot tell two builds apart: it stays at one value
across many unreleased changes, and a change to check logic can move verdicts
without touching any rule, profile or schema version. So the identity of the
running build is the package version plus, when the code runs from a git
checkout, the commit and whether the package source differs from it.

This is the single authority for that identity. The MCP handshake, every
assessment's ``generator`` block and ingest provenance all read it from here.
It never goes into ``manifest.json``, which must stay byte-identical across
builds that read the same bytes the same way.
"""

from __future__ import annotations

import re
import subprocess
from functools import lru_cache
from pathlib import Path
from typing import Any

from . import __version__

_PACKAGE_DIR = Path(__file__).resolve().parent
_COMMIT = re.compile(r"^[0-9a-f]{40}$")
_GIT_TIMEOUT_SECONDS = 5


def _checkout_root() -> Path | None:
    """The repository root if this package runs from a source checkout's ``src/``."""
    src = _PACKAGE_DIR.parent
    root = src.parent
    if src.name == "src" and (root / ".git").exists() and (root / "pyproject.toml").is_file():
        return root
    return None


def _git(root: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT_SECONDS,
        check=True,
    )
    return completed.stdout


def build_identity() -> dict[str, Any]:
    """The running build: version, and commit plus a dirty flag when known.

    ``commit`` and ``dirty`` are ``None`` when they cannot be established, and
    ``commit_unavailable`` then says why. ``dirty`` covers the package source
    only (``src/``), including untracked files, because that is what decides
    behaviour. Computed once per process (the code a process runs is the code
    it loaded); each call returns a fresh copy for the caller to keep.
    """
    return dict(_identity())


@lru_cache(maxsize=1)
def _identity() -> dict[str, Any]:
    identity: dict[str, Any] = {"version": __version__, "commit": None, "dirty": None}
    root = _checkout_root()
    if root is None:
        identity["commit_unavailable"] = "not running from a git checkout"
        return identity
    try:
        commit = _git(root, "rev-parse", "HEAD").strip()
        status = _git(root, "status", "--porcelain", "--", "src")
    except FileNotFoundError:
        identity["commit_unavailable"] = "git is not installed"
        return identity
    except (OSError, subprocess.SubprocessError) as error:
        identity["commit_unavailable"] = f"git failed: {type(error).__name__}"
        return identity
    if not _COMMIT.match(commit):
        identity["commit_unavailable"] = "git did not report a commit"
        return identity
    identity["commit"] = commit
    identity["dirty"] = bool(status.strip())
    return identity


def build_version() -> str:
    """The build as one version string, for places that take only a string.

    A PEP 440 local version: ``0.1.0+g<12-hex commit>``, with ``.dirty`` when the
    package source has uncommitted changes, or the bare package version when no
    commit is known.
    """
    identity = build_identity()
    if identity["commit"] is None:
        return identity["version"]
    local = "g" + identity["commit"][:12] + (".dirty" if identity["dirty"] else "")
    return f"{identity['version']}+{local}"
