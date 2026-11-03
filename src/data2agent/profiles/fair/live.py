"""Bounded, opt-in checks of public publication URLs.

Each DNS result is checked for a globally routable address, and the request
connects to that checked address. Redirects are checked anew. URL user-info and
query strings are rejected. These observations prove only what the HTTP
exchange showed; callers must separately bind the URL to the assessed dataset.
"""

from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
from datetime import datetime, timezone
from typing import Any
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit

_MAX_REDIRECTS = 3
_MAX_BODY = 65_536
_TIMEOUT_SECONDS = 6


class PublicURLRequired(ValueError):
    """The supplied URL is unsuitable for a public network probe."""


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, host: str, address: str, port: int):
        super().__init__(host, port=port, timeout=_TIMEOUT_SECONDS)
        self._checked_address = address

    def connect(self) -> None:
        self.sock = socket.create_connection(
            (self._checked_address, self.port), timeout=self.timeout
        )


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host: str, address: str, port: int):
        super().__init__(
            host, port=port, timeout=_TIMEOUT_SECONDS, context=ssl.create_default_context()
        )
        self._checked_address = address

    def connect(self) -> None:
        plain = socket.create_connection((self._checked_address, self.port), timeout=self.timeout)
        self.sock = self._context.wrap_socket(plain, server_hostname=self.host)


def _safe_url(value: str) -> tuple[str, str, int, str]:
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise PublicURLRequired("publication URL must not contain control characters")
    if len(value) > 2048:
        raise PublicURLRequired("publication URL is too long")
    # http.client sends only ASCII; IDNA hosts and percent-encoding are the caller's choice.
    if not value.isascii():
        raise PublicURLRequired(
            "publication URL must be ASCII: percent-encode the path and give the host in IDNA form"
        )
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
    except ValueError as error:
        raise PublicURLRequired("publication URL is not a valid URL") from error
    if parsed.scheme.lower() not in {"http", "https"} or not hostname:
        raise PublicURLRequired("publication URL must be HTTP or HTTPS")
    if parsed.username or parsed.password:
        raise PublicURLRequired("publication URL must not contain credentials")
    if parsed.query:
        raise PublicURLRequired("publication URL must not contain a query string")
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
    except ValueError as error:
        raise PublicURLRequired("publication URL has an invalid port") from error
    if port != (443 if parsed.scheme.lower() == "https" else 80):
        raise PublicURLRequired("publication URL must use the standard HTTP or HTTPS port")
    host = hostname.rstrip(".").lower()
    if not host or host == "localhost":
        raise PublicURLRequired("publication URL must use a public host")
    clean = urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path or "/", parsed.query, ""))
    return clean, host, port, parsed.scheme.lower()


def matching_declared_doi(value: str, declared_identifiers: list[str]) -> str | None:
    """Return a dataset DOI only when its exact resolver URL was supplied."""
    parsed = urlsplit(value)
    if parsed.scheme.lower() != "https" or (parsed.hostname or "").lower() != "doi.org":
        return None
    if parsed.query or parsed.username or parsed.password or parsed.port not in {None, 443}:
        return None
    doi_in_url = unquote(parsed.path).lstrip("/").lower()
    for identifier in declared_identifiers:
        candidate = identifier.lower().strip()
        for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
            if candidate.startswith(prefix):
                candidate = candidate[len(prefix) :]
                break
        if candidate.startswith("10.") and candidate == doi_in_url:
            return identifier
    return None


def bound_doi_url(value: str, declared_identifiers: list[str]) -> bool:
    """True only for the DOI resolver URL of a locally declared dataset DOI."""
    return matching_declared_doi(value, declared_identifiers) is not None


def _redirect_target(current: str, location: str) -> str:
    """The checked absolute URL of a redirect; a malformed one is refused like a private one."""
    try:
        target = urljoin(current, location)
    except ValueError as error:
        raise PublicURLRequired("redirect target is not a valid URL") from error
    _safe_url(target)
    return target


def _public_address(host: str, port: int) -> str:
    try:
        records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except OSError as error:
        raise PublicURLRequired("publication host could not be resolved") from error
    addresses = [record[4][0] for record in records]
    if not addresses or any(not ipaddress.ip_address(address).is_global for address in addresses):
        raise PublicURLRequired("publication host resolves to a non-public address")
    return addresses[0]


def _fetch_once(value: str) -> dict[str, Any]:
    response = _request(value, accept="text/html,application/json,*/*", max_body=_MAX_BODY)
    body = response.pop("body")
    return {**response, "body_sample": body[:_MAX_BODY].decode("utf-8", errors="replace")}


def _request(value: str, *, accept: str, max_body: int) -> dict[str, Any]:
    """One GET to a checked public address; reads at most ``max_body + 1`` bytes.

    The extra byte lets a caller tell a body that fits from one that was cut.
    """
    clean, host, port, scheme = _safe_url(value)
    address = _public_address(host, port)
    parsed = urlsplit(clean)
    target = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
    connection = (
        _PinnedHTTPSConnection(host, address, port)
        if scheme == "https"
        else _PinnedHTTPConnection(host, address, port)
    )
    try:
        connection.request(
            "GET",
            target,
            headers={
                "Host": parsed.netloc,
                "User-Agent": "Data2Agent-FAIR/0.1",
                "Accept": accept,
                "Range": f"bytes=0-{max_body}",
                "Connection": "close",
            },
        )
        response = connection.getresponse()
        return {
            "url": clean,
            "status_code": response.status,
            "location": response.getheader("Location"),
            "content_type": response.getheader("Content-Type"),
            "body": response.read(max_body + 1),
        }
    finally:
        connection.close()


def check_public_url(value: str) -> str:
    """Reject an unsuitable URL without any network access; return its clean form.

    A host name is checked again when it is resolved; an address literal can be
    refused now.
    """
    clean, host, _, _ = _safe_url(value)
    try:
        literal = ipaddress.ip_address(host.strip("[]"))
    except ValueError:
        return clean
    if not literal.is_global:
        raise PublicURLRequired("publication URL must use a public host")
    return clean


def fetch_public_document(value: str, *, accept: str, max_body: int) -> dict[str, Any]:
    """Fetch one public document with the probe's address and redirect checks.

    Unlike ``probe_public_url`` this keeps the body (at most ``max_body`` bytes,
    with ``truncated`` set when more was sent), so it can be frozen as evidence.
    A failed exchange is reported, never raised: a timeout is an observation.
    """
    clean = check_public_url(value)
    observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    chain: list[dict[str, Any]] = []
    current = clean
    response: dict[str, Any] | None = None
    try:
        for _ in range(_MAX_REDIRECTS + 1):
            response = _request(current, accept=accept, max_body=max_body)
            chain.append(
                {key: response[key] for key in ("url", "status_code", "location", "content_type")}
            )
            if response["status_code"] in {301, 302, 303, 307, 308} and response["location"]:
                current = _redirect_target(current, response["location"])
                continue
            break
    except (
        OSError,
        ssl.SSLError,
        http.client.HTTPException,
        PublicURLRequired,
        UnicodeError,
    ) as error:
        return {
            "source": clean,
            "observed_at": observed_at,
            "result": "network_error",
            "error_type": type(error).__name__,
            "chain": chain,
        }
    assert response is not None
    body = response["body"]
    return {
        "source": clean,
        "observed_at": observed_at,
        "result": "reached" if 200 <= response["status_code"] < 300 else "not_reached",
        "status_code": response["status_code"],
        "final_url": response["url"],
        "content_type": response["content_type"],
        "chain": chain,
        "body": body[:max_body],
        "truncated": len(body) > max_body,
    }


def probe_public_url(value: str, *, expected_identifier: str | None = None) -> dict[str, Any]:
    """Observe a public HTTP route without claiming dataset identity from status alone."""
    clean, _, _, _ = _safe_url(value)
    observed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    chain: list[dict[str, Any]] = []
    current = clean
    try:
        for _ in range(_MAX_REDIRECTS + 1):
            response = _fetch_once(current)
            chain.append(
                {key: response[key] for key in ("url", "status_code", "location", "content_type")}
            )
            if response["status_code"] in {301, 302, 303, 307, 308}:
                location = response["location"]
                if not location:
                    break
                current = _redirect_target(current, location)
                continue
            break
    except (OSError, ssl.SSLError, http.client.HTTPException) as error:
        return {
            "method": "bounded-public-http-get",
            "source": clean,
            "observed_at": observed_at,
            "result": "network_error",
            "error_type": type(error).__name__,
            "chain": chain,
        }
    final = response if chain else None
    reached = bool(final and 200 <= final["status_code"] < 300)
    identifier_seen = bool(
        reached
        and expected_identifier
        and re.search(
            rf"(?<![A-Za-z0-9._/-]){re.escape(expected_identifier)}(?![A-Za-z0-9._/-])",
            final["body_sample"],
            flags=re.IGNORECASE,
        )
    )
    return {
        "method": "bounded-public-http-get",
        "source": clean,
        "observed_at": observed_at,
        "result": "reached" if reached else "not_reached",
        "final_url": final["url"] if final else None,
        "status_code": final["status_code"] if final else None,
        "identifier_seen_in_sample": identifier_seen,
        "body_sample_bytes": (
            min(len(final["body_sample"].encode("utf-8")), _MAX_BODY) if final else 0
        ),
        "chain": chain,
    }
