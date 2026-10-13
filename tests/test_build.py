"""The running build's identity (#33)."""

from __future__ import annotations

import re
import subprocess

import pytest

from data2agent import __version__, build

_SHA = "0123456789abcdef0123456789abcdef01234567"


@pytest.fixture(autouse=True)
def _fresh_identity():
    build._identity.cache_clear()
    yield
    build._identity.cache_clear()


def _fake_git(monkeypatch, *, commit: str = _SHA, status: str = "") -> None:
    def git(root, *args):
        return commit + "\n" if args[0] == "rev-parse" else status

    monkeypatch.setattr(build, "_git", git)


def test_this_checkout_reports_a_consistent_identity():
    identity = build.build_identity()
    assert identity["version"] == __version__
    if identity["commit"] is None:
        assert identity["dirty"] is None
        assert identity["commit_unavailable"]
    else:
        assert re.fullmatch(r"[0-9a-f]{40}", identity["commit"])
        assert isinstance(identity["dirty"], bool)
        assert "commit_unavailable" not in identity


def test_a_clean_checkout_gives_a_local_version_with_the_commit(monkeypatch):
    _fake_git(monkeypatch)
    assert build.build_identity() == {"version": __version__, "commit": _SHA, "dirty": False}
    assert build.build_version() == f"{__version__}+g{_SHA[:12]}"


def test_uncommitted_package_changes_mark_the_build_dirty(monkeypatch):
    _fake_git(monkeypatch, status=" M src/data2agent/profiles/fair/checks.py\n")
    assert build.build_identity()["dirty"] is True
    assert build.build_version() == f"{__version__}+g{_SHA[:12]}.dirty"


def test_an_installed_package_reports_the_version_and_says_why_there_is_no_commit(monkeypatch):
    monkeypatch.setattr(build, "_checkout_root", lambda: None)
    identity = build.build_identity()
    assert identity == {
        "version": __version__,
        "commit": None,
        "dirty": None,
        "commit_unavailable": "not running from a git checkout",
    }
    assert build.build_version() == __version__


def test_missing_git_is_reported_not_raised(monkeypatch):
    def missing(*args, **kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(build, "_checkout_root", lambda: build._PACKAGE_DIR.parent.parent)
    monkeypatch.setattr(build.subprocess, "run", missing)
    identity = build.build_identity()
    assert identity["commit"] is None and identity["dirty"] is None
    assert identity["commit_unavailable"] == "git is not installed"


def test_a_failing_git_is_reported_not_raised(monkeypatch):
    def fails(*args, **kwargs):
        raise subprocess.CalledProcessError(128, ["git"])

    monkeypatch.setattr(build, "_checkout_root", lambda: build._PACKAGE_DIR.parent.parent)
    monkeypatch.setattr(build.subprocess, "run", fails)
    identity = build.build_identity()
    assert identity["commit"] is None and identity["dirty"] is None
    assert identity["commit_unavailable"] == "git failed: CalledProcessError"


def test_garbage_from_git_is_not_recorded_as_a_commit(monkeypatch):
    _fake_git(monkeypatch, commit="fatal: not a git repository")
    identity = build.build_identity()
    assert identity["commit"] is None
    assert identity["commit_unavailable"] == "git did not report a commit"


def test_each_caller_gets_its_own_copy(monkeypatch):
    _fake_git(monkeypatch)
    build.build_identity()["version"] = "tampered"
    assert build.build_identity()["version"] == __version__
