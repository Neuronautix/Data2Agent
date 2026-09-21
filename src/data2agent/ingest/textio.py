"""Text decoding, with the byte-order mark handled once and honestly.

A UTF-8 BOM decodes cleanly as UTF-8 and leaves a ``\\ufeff`` at the start of the
text, so a naive ``utf-8`` then ``utf-8-sig`` fallback never reaches the
fallback: the first decoder already succeeded. The result is a first column
named ``\\ufeffanimal_id`` propagating into the manifest, the evidence ledger and
every claim about that column -- and spreadsheet exports, which is most real
scientific tabular data, carry a BOM routinely.

So the BOM is detected from the bytes rather than discovered by a decode
failure, and the encoding we report is the one actually used.
"""

from __future__ import annotations

import codecs
from pathlib import Path

UTF8_BOM = codecs.BOM_UTF8


def decode(raw: bytes) -> tuple[str | None, str]:
    """Decode bytes as UTF-8, stripping a leading BOM. ``(None, "")`` if not UTF-8."""
    if raw.startswith(UTF8_BOM):
        try:
            return raw[len(UTF8_BOM) :].decode("utf-8"), "utf-8-sig"
        except UnicodeDecodeError:
            return None, ""
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return None, ""


def read_text(path: Path, *, limit: int | None = None) -> tuple[str | None, str]:
    """Read a file as UTF-8 text, BOM stripped. ``(None, "")`` if it cannot be read."""
    try:
        with path.open("rb") as handle:
            # A BOM is 3 bytes, so a bounded read must allow for it to still
            # return the requested number of characters' worth of content.
            raw = handle.read(limit + len(UTF8_BOM)) if limit is not None else handle.read()
    except OSError:
        return None, ""
    return decode(raw)
