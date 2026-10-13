"""Shared fixtures.

The example dataset is treated as a read-only fixture: tests that need to mutate
a dataset copy it into ``tmp_path`` first. A test that modified
``examples/preclinical-minimal`` would change its ``dataset_id`` and silently
invalidate every other test's expected numbers.
"""

from __future__ import annotations

import ipaddress
import shutil
import socket
from pathlib import Path

import pytest

from data2agent.ingest import ingest

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_DATASET = REPO_ROOT / "examples" / "preclinical-minimal"


class NetworkBlocked(AssertionError):
    """A test tried to leave the machine (#87).

    Deliberately not an ``OSError``: production code that handles connection
    failures (``profiles/fair/live.py`` does) would otherwise report it as an
    unreachable host instead of failing the test.
    """


def _is_local(host: object) -> bool:
    if host is None or host in ("", "localhost", b"localhost"):
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    try:
        return ipaddress.ip_address(str(host).split("%", 1)[0]).is_loopback
    except ValueError:
        return False


class NetworkGuard:
    """Blocks and records every non-loopback connection or name lookup.

    Loopback stays open because asyncio needs it: on Windows the event loop's
    self-pipe is a socket pair connected over 127.0.0.1. Every blocked attempt
    is recorded as well as raised, so code that swallows the exception still
    fails the test at teardown.
    """

    def __init__(self) -> None:
        self.attempts: list[str] = []

    def _block(self, what: str) -> None:
        self.attempts.append(what)
        raise NetworkBlocked(f"test tried to reach the network: {what}")

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real_connect = socket.socket.connect
        real_connect_ex = socket.socket.connect_ex
        real_getaddrinfo = socket.getaddrinfo

        def connect(sock, address):
            if isinstance(address, tuple) and not _is_local(address[0]):
                self._block(f"connect {address!r}")
            return real_connect(sock, address)

        def connect_ex(sock, address):
            if isinstance(address, tuple) and not _is_local(address[0]):
                self._block(f"connect_ex {address!r}")
            return real_connect_ex(sock, address)

        def getaddrinfo(host, *args, **kwargs):
            if not _is_local(host):
                self._block(f"getaddrinfo {host!r}")
            return real_getaddrinfo(host, *args, **kwargs)

        monkeypatch.setattr(socket.socket, "connect", connect)
        monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
        monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


@pytest.fixture(autouse=True)
def network_guard(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch):
    """Every test runs offline unless it is marked ``allow_network`` (#87)."""
    guard = NetworkGuard()
    if request.node.get_closest_marker("allow_network") is None:
        guard.install(monkeypatch)
    yield guard
    assert not guard.attempts, f"blocked network access was swallowed: {guard.attempts}"


@pytest.fixture(scope="session")
def example_dataset() -> Path:
    assert EXAMPLE_DATASET.is_dir(), f"example dataset missing at {EXAMPLE_DATASET}"
    return EXAMPLE_DATASET


@pytest.fixture
def dataset_copy(example_dataset: Path, tmp_path: Path) -> Path:
    """A writable copy of the example dataset, for tests that need to disturb it."""
    destination = tmp_path / "dataset"
    shutil.copytree(example_dataset, destination)
    return destination


@pytest.fixture
def ingested(example_dataset: Path, tmp_path: Path):
    return ingest(example_dataset, tmp_path / "out")


@pytest.fixture
def anyio_backend() -> str:
    """The MCP binding tests are async; asyncio is the only backend we need."""
    return "asyncio"
