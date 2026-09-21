"""Persistent-identifier extraction.

Identifiers are *detected*, never minted and never resolved offline. Each hit
records where it was found so that any later claim about identification can point
back at a byte range in a checksummed file. A detected DOI string is not a claim
that the DOI resolves -- that check belongs to the FAIR profile, in a later
version, and is explicitly out of scope here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .textio import read_text

# Scheme -> pattern. Conservative by design: a false negative is a warning, a
# false positive is a fabricated identifier.
_PATTERNS: dict[str, re.Pattern[str]] = {
    "doi": re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Za-z0-9]+\b"),
    "orcid": re.compile(r"\b\d{4}-\d{4}-\d{4}-\d{3}[\dX]\b"),
    "rrid": re.compile(r"\bRRID:\s*[A-Za-z_]+:[A-Za-z0-9_.\-]+\b"),
    "ror": re.compile(r"\bhttps?://ror\.org/0[0-9a-hjkmnp-tv-z]{6}\d{2}\b"),
    "uri": re.compile(r"\bhttps?://[^\s\"'<>)\]]+"),
}

_MAX_SCAN_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class IdentifierHit:
    scheme: str
    value: str
    source: str
    line: int

    def as_dict(self) -> dict[str, object]:
        return {
            "scheme": self.scheme,
            "value": self.value,
            "source": self.source,
            "line": self.line,
        }


def scan_text_file(path: Path, relative_path: str) -> list[IdentifierHit]:
    """Scan a text file for identifiers, recording the line each hit came from."""
    text, _ = read_text(path, limit=_MAX_SCAN_BYTES)
    if text is None:
        return []

    hits: list[IdentifierHit] = []
    seen: set[tuple[str, str]] = set()
    for line_number, line in enumerate(text.splitlines(), start=1):
        for scheme, pattern in _PATTERNS.items():
            for match in pattern.finditer(line):
                value = match.group(0).rstrip(".,;")
                if scheme == "uri" and _is_covered_by_specific_scheme(value):
                    continue
                key = (scheme, value)
                if key in seen:
                    continue
                seen.add(key)
                hits.append(IdentifierHit(scheme, value, relative_path, line_number))
    return sorted(hits, key=lambda hit: (hit.line, hit.scheme, hit.value))


def _is_covered_by_specific_scheme(value: str) -> bool:
    """Avoid double-reporting a DOI/ROR URL as a bare URI."""
    return "doi.org/" in value or value.startswith("https://ror.org/")


# Anchored forms of the scan patterns, for validating a single supplied value
# rather than finding one inside a line of text.
_ANCHORED: dict[str, re.Pattern[str]] = {
    "doi": re.compile(r"^10\.\d{4,9}/[-._;()/:A-Za-z0-9]+$"),
    "orcid": re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$"),
    "rrid": re.compile(r"^RRID:\s*[A-Za-z_]+:[A-Za-z0-9_.\-]+$"),
    "ror": re.compile(r"^https?://ror\.org/0[0-9a-hjkmnp-tv-z]{6}\d{2}$"),
    "uri": re.compile(r"^https?://[^\s\"'<>)\]]+$"),
}


def validate(value: str) -> dict[str, object]:
    """Check a single identifier's syntax against every known scheme.

    Syntax only. This says whether a string is shaped like a DOI, never whether
    that DOI exists -- resolution needs the network, which no check in this
    version makes.
    """
    candidate = value.strip()
    matched = [scheme for scheme, pattern in _ANCHORED.items() if pattern.match(candidate)]
    # A ROR or DOI URL also matches the generic URI pattern; the specific scheme
    # is the more useful of two true answers.
    if len(matched) > 1:
        matched = [scheme for scheme in matched if scheme != "uri"]

    checksum = _orcid_checksum(candidate) if "orcid" in matched else None
    return {
        "value": candidate,
        "syntactically_valid": bool(matched) and checksum is not False,
        "schemes": matched,
        **({"checksum_valid": checksum} if checksum is not None else {}),
    }


def _orcid_checksum(value: str) -> bool | None:
    """Verify an ORCID's ISO 7064 MOD 11-2 check digit."""
    digits = value.replace("-", "")
    if len(digits) != 16:
        return None
    total = 0
    for character in digits[:-1]:
        total = (total + int(character)) * 2
    expected = (12 - total % 11) % 11
    return ("X" if expected == 10 else str(expected)) == digits[-1].upper()
