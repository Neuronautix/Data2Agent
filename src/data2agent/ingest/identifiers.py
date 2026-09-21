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
    try:
        with path.open("rb") as handle:
            raw = handle.read(_MAX_SCAN_BYTES)
        text = raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
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
