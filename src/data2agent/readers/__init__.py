"""Optional readers for formats the dependency-free ingest core cannot parse.

These live outside ``data2agent.ingest`` deliberately. The ingest core needs
nothing but the standard library (D2A-70), and a ten-year-reproducible core is
worth more than convenience. A reader that needs a third-party parser belongs
here, behind an optional extra, and the core must remain honest about what it
cannot see when that extra is absent -- reporting the gap, never silence.
"""

from __future__ import annotations

__all__ = ["workbook"]
