"""The package version is declared twice; the two declarations must agree.

v0.2.0 was tagged while both still read 0.1.0, so an installed v0.2.0 reported
itself as 0.1.0 in ``--version``, in provenance and in the MCP handshake. The
git tag is not checked here (a test cannot know the tag); the two places a
running build can read its version from are.
"""

from __future__ import annotations

import re
from pathlib import Path

from data2agent import __version__

_ROOT = Path(__file__).resolve().parent.parent


def test_pyproject_and_the_package_declare_the_same_version():
    text = (_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    declared = re.search(r'^version\s*=\s*"([^"]+)"', text, re.MULTILINE)
    assert declared, "pyproject.toml declares no static version"
    assert declared.group(1) == __version__


def test_the_changelog_has_an_entry_for_the_current_version():
    changelog = (_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert re.search(rf"^## \[{re.escape(__version__)}\]", changelog, re.MULTILINE), (
        f"CHANGELOG.md has no '## [{__version__}]' heading"
    )
