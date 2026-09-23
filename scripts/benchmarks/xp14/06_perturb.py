"""Materialise the XP14 controlled perturbation suite as derived datasets (issue #20).

`benchmarks/xp14_apa/perturbations/perturbations.yaml` holds 16 specifications and
`applied: false`. This script turns each specification into its own addressable
dataset, written outside the frozen package, and records what the derived case is
supposed to demonstrate. The frozen snapshot is opened read-only and is verified
byte-for-byte before and after every run.

Three properties are enforced rather than asserted in prose:

1. *The parent is immutable.* The (path, sha256) set of `source/` is captured
   before the run and compared after it. A single changed byte aborts with the
   offending path named.
2. *Derived identity is reproducible.* Nothing here consults the clock, the
   filesystem order, or a random number generator, so re-running produces the same
   `derived_dataset_id`. `--verify` proves it by materialising the whole suite a
   second time into a temporary directory and comparing every digest.
3. *Gold comes from the specification.* The `gold` block of each provenance record
   is copied out of the YAML. Nothing in it is read back off the perturbed bytes,
   because an expectation inferred from the run would agree with the run by
   construction and would test nothing.

Editing strategy
    Most targets are cells inside OOXML workbooks, which XP14 usually names `.xls`
    (XP14-A001). Those are edited by surgery on the package XML and reassembled
    with `rewrite_zip`, which copies every untouched member's *already compressed*
    bytes verbatim. Loading and re-saving through openpyxl was rejected: it would
    silently drop the six charts in `230807_PT_MM.xls`, the pivot cache in
    `APA-ALL-results_MM.xlsx` and every `printerSettings` part, so a case meant to
    perturb one cell would perturb the whole file. openpyxl is still used, but only
    to read the result back and prove the perturbation landed -- always from a file
    handle, never from a path, because it dispatches on the extension.

Usage
    python scripts/benchmarks/xp14/06_perturb.py --list
    python scripts/benchmarks/xp14/06_perturb.py [--out DIR] [--only ID ...]
    python scripts/benchmarks/xp14/06_perturb.py --verify
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import zipfile
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape
from xml.sax.saxutils import unescape as xml_unescape


def _repo_root() -> Path:
    """Repository root, derived from this file's location.

    Keeps the generators runnable on any machine and inside CI. Override with
    the D2A_REPO environment variable if the scripts are vendored elsewhere.
    """
    env = os.environ.get("D2A_REPO")
    return Path(env).resolve() if env else Path(__file__).resolve().parents[3]


REPO = _repo_root()
sys.path.insert(0, str(REPO / "src"))

from data2agent.ingest.checksum import dataset_id as fold_dataset_id  # noqa: E402
from data2agent.ingest.checksum import hash_bytes, hash_file  # noqa: E402

try:
    import yaml
except ImportError:
    sys.exit("pyyaml is required: pip install pyyaml")

try:
    from openpyxl import load_workbook
except ImportError:
    sys.exit("openpyxl is required: pip install openpyxl")

PKG = REPO / "benchmarks" / "xp14_apa"
SRC = PKG / "source"
SPEC_FILE = PKG / "perturbations" / "perturbations.yaml"
DEFAULT_OUT = REPO / "benchmarks" / "xp14_perturbed"

# Bumped whenever a builder changes what it writes. A derived_dataset_id is only
# comparable across runs of the same generator_version.
GENERATOR_VERSION = "06_perturb.py/1.0.0"

GREEN, RED, RESET = "\033[32m", "\033[31m", "\033[0m"

# Named rather than inlined: these three literals have to match the bytes in the
# workbooks exactly, and a stray transliteration at a call site would be a silent
# no-match rather than an error. This file is UTF-8; do not re-encode it.
SHEET_RESUME = "Résumé"  # the session metadata sheet of every raw export
SHEET_RESULTATS = "Résultats"  # the sheet holding the 20 result-variable blocks
GREEK_DELTA_GENOTYPE = "ΔC/ΔC"  # the Unicode rendering XP14-P03 injects

BATCH2_DIR = "230904-08_APA-2_3M++-6M-dCdC"
BATCH1_DIR = "230807-11_APA-1_3M-3F-dCdC (By M Marias)"
IDS_REGISTRY = "Batch APA-1&2 Results/IDs Batch Sex Group.xlsx"


class Unimplementable(Exception):
    """A specification that cannot be applied deterministically.

    Raised, never swallowed: the case is recorded as not_implemented with this
    message as its reason, so a gap in the suite is visible in the output rather
    than showing up as a silently missing directory.
    """


# --------------------------------------------------------------------------
# ZIP surgery
# --------------------------------------------------------------------------
#
# Python's zipfile cannot copy a member without decompressing and recompressing
# it, which would make every byte of a workbook depend on the local zlib build
# even for the 40 parts a perturbation does not touch. This writer re-emits each
# untouched member's raw deflate stream, so a derived workbook differs from its
# parent only inside the parts actually edited. Self-tested: with no
# replacements it reproduces all 35 OOXML files in the snapshot byte for byte.

_LFH, _CDH, _EOCD = b"PK\x03\x04", b"PK\x01\x02", b"PK\x05\x06"


def rewrite_zip(raw: bytes, replacements: dict[str, bytes]) -> bytes:
    """Return the archive with the named members replaced, all others copied raw."""
    zin = zipfile.ZipFile(io.BytesIO(raw))
    infos = zin.infolist()
    unknown = set(replacements) - {i.filename for i in infos}
    if unknown:
        raise KeyError(f"no such zip member(s): {sorted(unknown)}")

    out = io.BytesIO()
    emitted: list[tuple[int, int, int, int, int, int]] = []
    for info in infos:
        if info.flag_bits & 0x08:
            # Streamed entry: sizes live after the payload, so the local header
            # cannot be patched in place. No Office file in XP14 uses one.
            raise ValueError(f"{info.filename}: data descriptor not supported")
        off = info.header_offset
        if raw[off : off + 4] != _LFH:
            raise ValueError(f"{info.filename}: local header not found at {off}")
        nlen, elen = struct.unpack("<HH", raw[off + 26 : off + 30])
        name_b = raw[off + 30 : off + 30 + nlen]
        extra_b = raw[off + 30 + nlen : off + 30 + nlen + elen]
        data_at = off + 30 + nlen + elen
        blob = raw[data_at : data_at + info.compress_size]
        method, flags = info.compress_type, info.flag_bits
        crc, csize, usize = info.CRC, info.compress_size, info.file_size

        if info.filename in replacements:
            payload = replacements[info.filename]
            usize, crc = len(payload), zlib.crc32(payload) & 0xFFFFFFFF
            if method == zipfile.ZIP_DEFLATED:
                # Raw deflate (-15) at a fixed level: same input, same bytes.
                comp = zlib.compressobj(9, zlib.DEFLATED, -15)
                blob = comp.compress(payload) + comp.flush()
            else:
                blob = payload
            csize = len(blob)

        new_off = out.tell()
        version = struct.unpack("<H", raw[off + 4 : off + 6])[0]
        mtime, mdate = struct.unpack("<HH", raw[off + 10 : off + 14])
        out.write(
            _LFH
            + struct.pack(
                "<HHHHHIIIHH", version, flags, method, mtime, mdate, crc, csize, usize, nlen, elen
            )
        )
        out.write(name_b)
        out.write(extra_b)
        out.write(blob)
        emitted.append((new_off, flags, method, crc, csize, usize))

    # The central directory is copied record by record and patched, so extra
    # fields and external attributes survive exactly as Excel wrote them.
    cd_start = out.tell()
    pos = raw.find(_CDH)
    for (new_off, flags, method, crc, csize, usize), _ in zip(emitted, infos, strict=True):
        if raw[pos : pos + 4] != _CDH:
            raise ValueError("malformed central directory")
        nlen, elen, clen = struct.unpack("<HHH", raw[pos + 28 : pos + 34])
        rec = bytearray(raw[pos : pos + 46 + nlen + elen + clen])
        struct.pack_into("<HH", rec, 8, flags, method)
        struct.pack_into("<III", rec, 16, crc, csize, usize)
        struct.pack_into("<I", rec, 42, new_off)
        out.write(bytes(rec))
        pos += 46 + nlen + elen + clen

    eocd = bytearray(raw[raw.rfind(_EOCD) :])
    struct.pack_into("<II", eocd, 12, out.tell() - cd_start, cd_start)
    out.write(bytes(eocd))
    return out.getvalue()


class Workbook:
    """An OOXML package held as decoded XML parts until it is serialised back."""

    def __init__(self, raw: bytes) -> None:
        self.raw = raw
        self._zip = zipfile.ZipFile(io.BytesIO(raw))
        self._edits: dict[str, str] = {}

    def part(self, name: str) -> str:
        if name not in self._edits:
            self._edits[name] = self._zip.read(name).decode("utf-8")
        return self._edits[name]

    def set_part(self, name: str, text: str) -> None:
        self._edits[name] = text

    def sheet_part(self, sheet_name: str) -> str:
        """Resolve a sheet name to its package part through workbook.xml.rels.

        XP14's raw exports put the first sheet in the workbook order (Resume) in
        sheet1.xml and the second (Resultats) in sheet2.xml, but that is a
        coincidence of how the acquisition software saved them, not a rule. The
        relationship graph is the only reliable route.
        """
        wb = self.part("xl/workbook.xml")
        rid = None
        for entry in re.finditer(r'<sheet name="([^"]*)"[^>]*r:id="([^"]+)"[^>]*/>', wb):
            if xml_unescape(entry.group(1)) == sheet_name:
                rid = entry.group(2)
                break
        if rid is None:
            names = [xml_unescape(n) for n in re.findall(r'<sheet name="([^"]*)"', wb)]
            raise Unimplementable(f"sheet {sheet_name!r} not found; workbook has {names}")
        rels = self.part("xl/_rels/workbook.xml.rels")
        rel = re.search(rf'<Relationship Id="{re.escape(rid)}"[^>]*Target="([^"]+)"', rels)
        if not rel:
            raise Unimplementable(f"relationship {rid} missing from workbook.xml.rels")
        return "xl/" + rel.group(1).lstrip("/")

    def to_bytes(self) -> bytes:
        touched = {
            name: text.encode("utf-8")
            for name, text in self._edits.items()
            if text.encode("utf-8") != self._zip.read(name)
        }
        return rewrite_zip(self.raw, touched)


# --------------------------------------------------------------------------
# SpreadsheetML cell helpers
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Cell:
    start: int
    end: int
    attrs: str  # the whole open tag's attributes, r= first: ' r="F9" s="1" t="s"'
    inner: str  # "" for a self-closing cell


def find_cell(sheet_xml: str, ref: str) -> Cell:
    match = re.search(rf'<c r="{re.escape(ref)}"((?:\s[^>]*?)?)(/?)>', sheet_xml)
    if not match:
        raise Unimplementable(f"cell {ref} is absent from the sheet")
    # r= is re-attached here rather than captured, so that every writer below
    # rebuilds the tag with its address intact. A cell that loses r= is not
    # invalid XML -- readers fall back to positional inference -- which is
    # exactly why dropping it would corrupt a sheet without raising anything.
    attrs = f' r="{ref}"' + match.group(1)
    if match.group(2) == "/":
        return Cell(match.start(), match.end(), attrs, "")
    close = sheet_xml.index("</c>", match.end())
    return Cell(match.start(), close + 4, attrs, sheet_xml[match.end() : close])


def _attrs_without_type(attrs: str) -> str:
    return re.sub(r'\st="[^"]*"', "", attrs)


def cell_shared_index(sheet_xml: str, ref: str) -> int:
    cell = find_cell(sheet_xml, ref)
    if 't="s"' not in cell.attrs:
        raise Unimplementable(f"cell {ref} does not hold a shared string")
    value = re.search(r"<v>(\d+)</v>", cell.inner)
    if not value:
        raise Unimplementable(f"cell {ref} has no shared-string index")
    return int(value.group(1))


def cell_raw_value(sheet_xml: str, ref: str) -> str | None:
    cell = find_cell(sheet_xml, ref)
    value = re.search(r"<v>(.*?)</v>", cell.inner, re.S)
    return value.group(1) if value else None


def cell_formula(sheet_xml: str, ref: str) -> str | None:
    cell = find_cell(sheet_xml, ref)
    formula = re.search(r"<f[^>]*>(.*?)</f>", cell.inner, re.S)
    return formula.group(1) if formula else None


def _replace(sheet_xml: str, cell: Cell, replacement: str) -> str:
    return sheet_xml[: cell.start] + replacement + sheet_xml[cell.end :]


def set_shared_index(sheet_xml: str, ref: str, index: int) -> str:
    cell = find_cell(sheet_xml, ref)
    attrs = _attrs_without_type(cell.attrs) + ' t="s"'
    return _replace(sheet_xml, cell, f"<c{attrs}><v>{index}</v></c>")


def _col_key(ref: str) -> tuple[int, str]:
    """Sort key for a cell reference, by column letters then by length."""
    letters = re.match(r"([A-Z]+)", ref).group(1)
    return (len(letters), letters)


def add_shared_cells(sheet_xml: str, cells: dict[str, int]) -> str:
    """Write shared-string cells that may not exist yet, creating rows as needed.

    `set_shared_index` can only rewrite a cell the sheet already has. A visually
    blank row often has no <row> element at all -- XP14's INDEX sheet has none
    for rows 4 and 5 -- so "add a label to an empty row" is not a rewrite but an
    insertion, and rows and cells must both land in ascending order or readers
    silently mis-position them.
    """
    for ref, index in sorted(cells.items(), key=lambda kv: _col_key(kv[0])):
        row_number = int(re.search(r"(\d+)$", ref).group(1))
        tag = f'<c r="{ref}" t="s"><v>{index}</v></c>'

        row = re.search(rf'<row[^>]*\sr="{row_number}"[^>]*?(/)?>', sheet_xml)
        if row is None:
            # No such row: insert a fresh one before the first row that sorts
            # after it, or before </sheetData> when it belongs last.
            successor = None
            for candidate in re.finditer(r'<row[^>]*\sr="(\d+)"', sheet_xml):
                if int(candidate.group(1)) > row_number:
                    successor = candidate.start()
                    break
            at = successor if successor is not None else sheet_xml.index("</sheetData>")
            sheet_xml = sheet_xml[:at] + f'<row r="{row_number}">{tag}</row>' + sheet_xml[at:]
            continue

        if row.group(1) == "/":  # <row .../> -- present but empty
            open_tag = row.group(0)[:-2] + ">"
            sheet_xml = (
                sheet_xml[: row.start()] + open_tag + tag + "</row>" + sheet_xml[row.end() :]
            )
            continue

        close = sheet_xml.index("</row>", row.end())
        body = sheet_xml[row.end() : close]
        if f'<c r="{ref}"' in body:
            sheet_xml = set_shared_index(sheet_xml, ref, index)
            continue
        # Insert before the first cell whose column sorts after this one.
        at = close
        for candidate in re.finditer(r'<c r="([A-Z]+\d+)"', body):
            if _col_key(candidate.group(1)) > _col_key(ref):
                at = row.end() + candidate.start()
                break
        sheet_xml = sheet_xml[:at] + tag + sheet_xml[at:]
    return sheet_xml


def set_number(sheet_xml: str, ref: str, literal: str, style: str | None = None) -> str:
    """Write a numeric literal. `style` overrides the cell's s= index when given."""
    cell = find_cell(sheet_xml, ref)
    attrs = _attrs_without_type(cell.attrs)
    if style is not None:
        attrs = re.sub(r'\ss="[^"]*"', "", attrs) + f' s="{style}"'
    return _replace(sheet_xml, cell, f"<c{attrs}><v>{literal}</v></c>")


def set_cached_value(sheet_xml: str, ref: str, literal: str) -> str:
    """Refresh a formula cell's cached <v> and leave its <f> exactly as it was.

    Distinct from `set_number`, which replaces the whole cell body: using that on
    a formula cell would delete the formula, which for XP14-P14 is precisely the
    evidence the control has to preserve.
    """
    cell = find_cell(sheet_xml, ref)
    if "<f" not in cell.inner:
        raise Unimplementable(f"cell {ref} has no formula, so it has no cache to refresh")
    inner, n = re.subn(r"<v>.*?</v>", f"<v>{literal}</v>", cell.inner, count=1, flags=re.S)
    if n != 1:
        raise Unimplementable(f"cell {ref} has no cached value to refresh")
    return _replace(sheet_xml, cell, f"<c{cell.attrs}>{inner}</c>")


def delete_cell(sheet_xml: str, ref: str) -> str:
    """Remove the cell element entirely, which is how OOXML spells "blank".

    The row's `spans` hint is deliberately left alone: it is an optimisation hint
    that may over-cover, and narrowing it would be a second, unspecified edit.
    """
    return _replace(sheet_xml, find_cell(sheet_xml, ref), "")


def drop_formula(sheet_xml: str, ref: str) -> str:
    """Strip <f> and keep <v>: the value survives, its derivation does not."""
    cell = find_cell(sheet_xml, ref)
    if "<f" not in cell.inner:
        raise Unimplementable(f"cell {ref} carries no formula to drop")
    inner = re.sub(r"<f[^>]*/>|<f[^>]*>.*?</f>", "", cell.inner, flags=re.S)
    return _replace(sheet_xml, cell, f"<c{cell.attrs}>{inner}</c>")


def shared_string_text(sst_xml: str, index: int) -> str:
    items = re.findall(r"<si>(.*?)</si>", sst_xml, re.S)
    if index >= len(items):
        raise Unimplementable(f"shared string {index} out of range ({len(items)} entries)")
    return "".join(xml_unescape(t) for t in re.findall(r"<t[^>]*>(.*?)</t>", items[index], re.S))


def append_shared_string(sst_xml: str, text: str) -> tuple[str, int]:
    """Append a new <si> and return the updated table plus the new index.

    Existing entries are never edited in place. In this snapshot a single <si>
    backs many cells -- `dC/dC` alone backs nine cells of the registry -- so
    rewriting one would silently perturb every cell that shares it.
    """
    unique = int(re.search(r'uniqueCount="(\d+)"', sst_xml).group(1))
    space = ' xml:space="preserve"' if text != text.strip() else ""
    entry = f"<si><t{space}>{xml_escape(text)}</t></si>"
    sst_xml = sst_xml.replace("</sst>", entry + "</sst>")
    sst_xml = re.sub(r'uniqueCount="\d+"', f'uniqueCount="{unique + 1}"', sst_xml, count=1)
    return sst_xml, unique


def adjust_sst_count(sst_xml: str, delta: int) -> str:
    """`count` is the number of string *references*; deleting a cell lowers it."""
    total = int(re.search(r'\bcount="(\d+)"', sst_xml).group(1))
    return re.sub(r'\bcount="\d+"', f'count="{total + delta}"', sst_xml, count=1)


def drop_calcchain_entry(chain_xml: str, ref: str, sheet_index: str) -> str:
    """Remove one cell from the recalculation cache.

    calcChain is only a cache of evaluation order, but an entry pointing at a
    cell that no longer holds a formula is exactly the kind of internal
    inconsistency Excel offers to "repair" -- which would rewrite the file and
    destroy the perturbation.
    """
    pattern = rf'<c r="{re.escape(ref)}" i="{sheet_index}"[^>]*/>'
    hits = re.findall(pattern, chain_xml)
    if len(hits) != 1:
        raise Unimplementable(f"expected 1 calcChain entry for {ref}, found {len(hits)}")
    return chain_xml.replace(hits[0], "", 1)


def excel_serial(day: date) -> int:
    """Excel's 1900 date system, with its deliberate 1900-02-29 off-by-one."""
    return (day - date(1899, 12, 30)).days


# --------------------------------------------------------------------------
# Case model
# --------------------------------------------------------------------------


@dataclass
class Case:
    """One derived dataset: what to change, why, and how to check it landed."""

    renames: dict[str, str] = field(default_factory=dict)
    # No current specification removes a file, but the four `remove` items make
    # file deletion a plausible next perturbation, so the path is built and
    # reported rather than left to be bolted on by whoever needs it first.
    deletes: set[str] = field(default_factory=set)
    additions: dict[str, bytes] = field(default_factory=dict)
    edits: dict[str, Callable[[bytes], bytes]] = field(default_factory=dict)
    # Parameters the specification leaves open, with the value chosen and why.
    choices: list[dict[str, str]] = field(default_factory=list)
    # Places where the specification and the bytes disagree. Reported, not fixed.
    caveats: list[str] = field(default_factory=list)
    # Assertions run against the materialised case, as human-readable evidence.
    verify: Callable[[Path], list[str]] | None = None
    # True for the two cases that move bytes without altering any of them.
    bytes_preserved: bool = False


Builder = Callable[[dict], Case]
BUILDERS: dict[str, Builder] = {}


def builder(pid: str) -> Callable[[Builder], Builder]:
    def register(fn: Builder) -> Builder:
        BUILDERS[pid] = fn
        return fn

    return register


def _open_derived(root: Path, rel: str):
    """Load a derived workbook from bytes.

    Always by handle. Twenty XP14 files are OOXML behind a `.xls` name, and
    openpyxl dispatches on the extension when it is handed a path, so opening
    one of these by path raises before it ever looks at the signature.
    """
    return load_workbook(io.BytesIO((root / rel).read_bytes()))


def _open_derived_values(root: Path, rel: str):
    return load_workbook(io.BytesIO((root / rel).read_bytes()), data_only=True)


# --------------------------------------------------------------------------
# Builders: one per specification
# --------------------------------------------------------------------------


def _registry_string_swap(ref: str, expect: str, new_text: str) -> Callable[[bytes], bytes]:
    """Repoint one registry cell at a freshly appended shared string."""

    def apply(raw: bytes) -> bytes:
        wb = Workbook(raw)
        part = wb.sheet_part("Sheet1")
        sheet, sst = wb.part(part), wb.part("xl/sharedStrings.xml")
        observed = shared_string_text(sst, cell_shared_index(sheet, ref))
        if observed != expect:
            raise Unimplementable(f"{ref} holds {observed!r}, spec baseline says {expect!r}")
        sst, index = append_shared_string(sst, new_text)
        wb.set_part("xl/sharedStrings.xml", sst)
        wb.set_part(part, set_shared_index(sheet, ref, index))
        return wb.to_bytes()

    return apply


@builder("XP14-P01")
def _p01(spec: dict) -> Case:
    def apply(raw: bytes) -> bytes:
        wb = Workbook(raw)
        part = wb.sheet_part("Sheet1")
        sheet, sst = wb.part(part), wb.part("xl/sharedStrings.xml")
        observed = shared_string_text(sst, cell_shared_index(sheet, "F9"))
        if observed != "M":
            raise Unimplementable(f"F9 holds {observed!r}, spec baseline says 'M'")
        wb.set_part(part, delete_cell(sheet, "F9"))
        wb.set_part("xl/sharedStrings.xml", adjust_sst_count(sst, -1))
        return wb.to_bytes()

    def verify(root: Path) -> list[str]:
        ws = _open_derived(root, IDS_REGISTRY)["Sheet1"]
        assert ws["F9"].value is None, ws["F9"].value
        assert ws["F10"].value == "M", "a neighbouring sex must survive untouched"
        # A9 is a formula cell, so the animal it names is only in the cache.
        animal = _open_derived_values(root, IDS_REGISTRY)["Sheet1"]["A9"].value
        assert animal == "B2_1-I", animal
        return [f"Sheet1!F9 is empty for {animal}; Sheet1!F10 still 'M'"]

    return Case(edits={IDS_REGISTRY: apply}, verify=verify)


@builder("XP14-P02")
def _p02(spec: dict) -> Case:
    def verify(root: Path) -> list[str]:
        ws = _open_derived(root, IDS_REGISTRY)["Sheet1"]
        assert ws["E3"].value == "dc/dc", ws["E3"].value
        assert ws["E4"].value == "dC/dC", "only E3 may change"
        return ["Sheet1!E3 == 'dc/dc'; E4 still 'dC/dC'"]

    return Case(edits={IDS_REGISTRY: _registry_string_swap("E3", "dC/dC", "dc/dc")}, verify=verify)


@builder("XP14-P03")
def _p03(spec: dict) -> Case:
    def verify(root: Path) -> list[str]:
        ws = _open_derived(root, IDS_REGISTRY)["Sheet1"]
        assert ws["E4"].value == GREEK_DELTA_GENOTYPE, ws["E4"].value
        assert ws["E5"].value == "dC/dC", "only E4 may change"
        return [f"Sheet1!E4 == {GREEK_DELTA_GENOTYPE!r}; E5 still 'dC/dC'"]

    return Case(
        edits={IDS_REGISTRY: _registry_string_swap("E4", "dC/dC", GREEK_DELTA_GENOTYPE)},
        verify=verify,
    )


@builder("XP14-P08")
def _p08(spec: dict) -> Case:
    def verify(root: Path) -> list[str]:
        ws = _open_derived(root, IDS_REGISTRY)["Sheet1"]
        assert ws["F6"].value == "M", ws["F6"].value
        assert ws["F7"].value == "F" and ws["F8"].value == "F", "the other females must stay F"
        return ["Sheet1!F6 (B1_5-I) == 'M'; F7/F8 still 'F'"]

    return Case(
        edits={IDS_REGISTRY: _registry_string_swap("F6", "F", "M")},
        verify=verify,
        caveats=[
            "The conflict this creates is with APA-ALL-results_HD.xlsx!'Entries % calc' row 6, "
            "which is left untouched by design. The registry is the only edited side."
        ],
    )


_PT = f"{BATCH1_DIR}/Data/230807_PT.xls"
_PT_MM = f"{BATCH1_DIR}/Data/230807_PT_MM.xls"
_T1 = f"{BATCH1_DIR}/Data/230808_T1.xls"
_T2 = f"{BATCH1_DIR}/Data/230809_T2.xls"


def _resultats_string_swap(ref: str, expect: str, new_text: str) -> Callable[[bytes], bytes]:
    def apply(raw: bytes) -> bytes:
        wb = Workbook(raw)
        part = wb.sheet_part(SHEET_RESULTATS)
        sheet, sst = wb.part(part), wb.part("xl/sharedStrings.xml")
        observed = shared_string_text(sst, cell_shared_index(sheet, ref))
        if observed != expect:
            raise Unimplementable(f"{ref} holds {observed!r}, spec baseline says {expect!r}")
        sst, index = append_shared_string(sst, new_text)
        wb.set_part("xl/sharedStrings.xml", sst)
        wb.set_part(part, set_shared_index(sheet, ref, index))
        return wb.to_bytes()

    return apply


@builder("XP14-P04")
def _p04(spec: dict) -> Case:
    unit_less = "Kinetics_distance_X1_300s"

    def verify(root: Path) -> list[str]:
        ws = _open_derived(root, _PT)[SHEET_RESULTATS]
        assert ws["A2"].value == unit_less, ws["A2"].value
        assert ws["A12"].value and "(" in str(ws["A12"].value), "other variables keep their unit"
        return [f"Resultats!A2 == {unit_less!r}; A12 still carries a parenthesised unit"]

    return Case(
        edits={_PT: _resultats_string_swap("A2", f"{unit_less} (cm)", unit_less)},
        verify=verify,
    )


@builder("XP14-P05")
def _p05(spec: dict) -> Case:
    # The Resultats sheet repeats its 22_seaN header row once per result
    # variable, so '22_sea3' heads a column in twenty places. Perturbing only
    # D3 would leave nineteen rows still naming session 3, which is a different
    # defect (an inconsistency between blocks) from the one the spec intends.
    # The spec was corrected on 2026-09-23 to name all twenty; this follows it.
    rows = [3, 13, 23, 33, 43, 53, 63, 73, 83, 93, 103, 113, 123, 134, 145, 156, 167, 178, 189, 199]
    refs = [f"D{r}" for r in rows]

    def apply(raw: bytes) -> bytes:
        wb = Workbook(raw)
        part = wb.sheet_part(SHEET_RESULTATS)
        sheet, sst = wb.part(part), wb.part("xl/sharedStrings.xml")
        for ref in refs:
            observed = shared_string_text(sst, cell_shared_index(sheet, ref))
            if observed != "22_sea3":
                raise Unimplementable(f"{ref} holds {observed!r}, spec baseline says '22_sea3'")
        sst, index = append_shared_string(sst, "22_sea9")
        for ref in refs:
            sheet = set_shared_index(sheet, ref, index)
        wb.set_part("xl/sharedStrings.xml", sst)
        wb.set_part(part, sheet)
        return wb.to_bytes()

    def verify(root: Path) -> list[str]:
        ws = _open_derived(root, _PT)[SHEET_RESULTATS]
        stale = [ref for ref in refs if ws[ref].value != "22_sea9"]
        assert not stale, f"still 22_sea3 at {stale}"
        # The other five columns must be untouched, or the case tests nothing.
        assert ws["C3"].value == "22_sea2" and ws["E3"].value == "22_sea4", "neighbours moved"
        return [f"all {len(refs)} '22_sea3' header cells read '22_sea9'; neighbours unchanged"]

    return Case(edits={_PT: apply}, verify=verify)


@builder("XP14-P06")
def _p06(spec: dict) -> Case:
    cached = "147.33333333333334"

    def apply(raw: bytes) -> bytes:
        wb = Workbook(raw)
        part = wb.sheet_part(SHEET_RESULTATS)
        sheet = wb.part(part)
        formula, value = cell_formula(sheet, "B10"), cell_raw_value(sheet, "B10")
        if formula != "AVERAGE(B4:B9)":
            raise Unimplementable(f"B10 formula is {formula!r}, spec says '=AVERAGE(B4:B9)'")
        if value != cached:
            raise Unimplementable(f"B10 cached value is {value!r}, spec says {cached!r}")
        wb.set_part(part, drop_formula(sheet, "B10"))
        # calcChain addresses sheets by sheetId; Resultats is sheetId 1 here.
        wb.set_part(
            "xl/calcChain.xml", drop_calcchain_entry(wb.part("xl/calcChain.xml"), "B10", "1")
        )
        return wb.to_bytes()

    def verify(root: Path) -> list[str]:
        ws = _open_derived(root, _PT_MM)[SHEET_RESULTATS]
        assert ws["B10"].value == float(cached), ws["B10"].value
        assert str(ws["C10"].value).startswith("="), "the five siblings keep their formulas"
        values = _open_derived_values(root, _PT_MM)[SHEET_RESULTATS]
        bins = [values[f"B{r}"].value for r in range(4, 10)]
        assert abs(sum(bins) / 6 - float(cached)) < 1e-9, "the value must stay recomputable"
        return [
            f"Resultats!B10 holds the literal {cached} with no <f>; C10 still '=AVERAGE(C4:C9)'",
            f"mean(B4:B9) == {sum(bins) / 6!r}, so the value remains verifiable by recomputation",
        ]

    return Case(edits={_PT_MM: apply}, verify=verify)


@builder("XP14-P07")
def _p07(spec: dict) -> Case:
    def apply(raw: bytes) -> bytes:
        wb = Workbook(raw)
        part = wb.sheet_part(SHEET_RESUME)
        sheet = wb.part(part)
        observed = cell_raw_value(sheet, "B5")
        if observed != "4":
            raise Unimplementable(f"Resume!B5 holds {observed!r}, spec baseline says 4")
        wb.set_part(part, set_number(sheet, "B5", "3"))
        return wb.to_bytes()

    def verify(root: Path) -> list[str]:
        ws = _open_derived(root, _PT)[SHEET_RESUME]
        seances = [ws[f"B{r}"].value for r in range(2, 8)]
        assert seances == [1, 2, 3, 3, 5, 6], seances
        return [f"Resume!B2:B7 == {seances}; session 3 duplicated, session 4 vacated"]

    return Case(edits={_PT: apply}, verify=verify)


@builder("XP14-P10")
def _p10(spec: dict) -> Case:
    protocol = "Training_choc_0.3mA 30'.xls"

    def apply(raw: bytes) -> bytes:
        wb = Workbook(raw)
        part = wb.sheet_part(SHEET_RESUME)
        sheet, sst = wb.part(part), wb.part("xl/sharedStrings.xml")
        observed = shared_string_text(sst, cell_shared_index(sheet, "C2"))
        if observed != protocol:
            raise Unimplementable(f"Resume!C2 holds {observed!r}, expected {protocol!r}")
        wb.set_part(part, delete_cell(sheet, "C2"))
        wb.set_part("xl/sharedStrings.xml", adjust_sst_count(sst, -1))
        return wb.to_bytes()

    def verify(root: Path) -> list[str]:
        ws = _open_derived(root, _T1)[SHEET_RESUME]
        assert ws["C2"].value is None, ws["C2"].value
        rest = [ws[f"C{r}"].value for r in range(3, 8)]
        assert rest == [protocol] * 5, rest
        return ["Resume!C2 is empty; C3:C7 still name the 0.3 mA protocol file"]

    return Case(edits={_T1: apply}, verify=verify)


@builder("XP14-P11")
def _p11(spec: dict) -> Case:
    # The acquisition software writes these timestamps with a leading space; the
    # spec quotes them without it. The space is preserved, so the only difference
    # between the parent and the derived value is the year.
    before, after = " 09/08/2023 09:18:48", " 09/08/2024 09:18:48"

    def apply(raw: bytes) -> bytes:
        wb = Workbook(raw)
        part = wb.sheet_part(SHEET_RESUME)
        sheet, sst = wb.part(part), wb.part("xl/sharedStrings.xml")
        observed = shared_string_text(sst, cell_shared_index(sheet, "E2"))
        if observed != before:
            raise Unimplementable(f"Resume!E2 holds {observed!r}, expected {before!r}")
        sst, index = append_shared_string(sst, after)
        wb.set_part("xl/sharedStrings.xml", sst)
        wb.set_part(part, set_shared_index(sheet, "E2", index))
        return wb.to_bytes()

    def verify(root: Path) -> list[str]:
        ws = _open_derived(root, _T2)[SHEET_RESUME]
        assert ws["E2"].value == after, repr(ws["E2"].value)
        assert ws["E3"].value.strip().startswith("09/08/2023"), "the next session stays in 2023"
        return [f"Resume!E2 == {after!r}; E3 still in the 2023 batch window"]

    return Case(edits={_T2: apply}, verify=verify)


@builder("XP14-P09")
def _p09(spec: dict) -> Case:
    # Exactly the two assertions the spec names, and nothing else: a third
    # invented field would be a third thing for an agent to be graded on.
    payload = (
        json.dumps(
            {"Species": "NCBITaxon:00000", "Strain": "RRID:IMSR_JAX:0000000"},
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")

    def verify(root: Path) -> list[str]:
        doc = json.loads((root / "dataset_description.json").read_text(encoding="utf-8"))
        assert doc["Species"] == "NCBITaxon:00000"
        assert doc["Strain"] == "RRID:IMSR_JAX:0000000"
        return ["dataset_description.json at the dataset root carries both bogus identifiers"]

    return Case(
        additions={"dataset_description.json": payload},
        verify=verify,
        choices=[
            {
                "parameter": "file location",
                "value": "dataset root",
                "why": "the spec writes '(new file) dataset_description.json' with no directory; "
                "the root is where a dataset-level descriptor is looked for",
            }
        ],
    )


@builder("XP14-P12")
def _p12(spec: dict) -> Case:
    payload = (
        b"SPDX-License-Identifier: CC-BY-4.0\n"
        b"\n"
        b"This dataset is licensed under the Creative Commons Attribution 4.0\n"
        b"International License (CC BY 4.0).\n"
        b"\n"
        b"Full licence text: https://creativecommons.org/licenses/by/4.0/legalcode\n"
    )

    def verify(root: Path) -> list[str]:
        text = (root / "LICENSE").read_text(encoding="utf-8")
        assert "CC-BY-4.0" in text and "creativecommons.org" in text
        return ["LICENSE at the dataset root declares CC-BY-4.0 by SPDX identifier and URL"]

    return Case(
        additions={"LICENSE": payload},
        verify=verify,
        choices=[
            {
                "parameter": "licence body",
                "value": "SPDX identifier, name and canonical URL",
                "why": "the spec asks for 'a CC-BY-4.0 licence file' without fixing its text; a "
                "short declaration is unambiguous and cannot be mistranscribed, whereas the "
                "7,000-word legal code could be",
            }
        ],
    )


@builder("XP14-P13")
def _p13(spec: dict) -> Case:
    new = f"{BATCH1_DIR}/Data/230807_PT.xlsx"

    def verify(root: Path) -> list[str]:
        assert (root / new).exists(), "renamed file missing"
        assert not (root / _PT).exists(), "the .xls name must be gone"
        head = (root / new).read_bytes()[:4]
        assert head == b"PK\x03\x04", head
        return [f"{Path(new).name} present, .xls name absent, signature still 50 4B 03 04"]

    return Case(renames={_PT: new}, verify=verify, bytes_preserved=True)


@builder("XP14-P15")
def _p15(spec: dict) -> Case:
    new_dir = "230904-08_APA-2_6M++-3M-dCdC"

    def verify(root: Path) -> list[str]:
        assert (root / new_dir).is_dir(), "renamed directory missing"
        assert not (root / BATCH2_DIR).exists(), "the stale directory name must be gone"
        moved = sorted(p.relative_to(root).as_posix() for p in (root / new_dir).rglob("*"))
        return [f"{new_dir}/ holds {len(moved)} entries; {BATCH2_DIR}/ is gone"]

    return Case(
        renames={BATCH2_DIR: new_dir},  # directory prefix; expanded at materialisation
        verify=verify,
        bytes_preserved=True,
    )


@builder("XP14-P14")
def _p14(spec: dict) -> Case:
    # The spec says "plausible dates" without naming any. One cohort date is used
    # for all nine animals rather than nine invented per-animal dates: the batch
    # was weighed on a single day and inventing nine distinct birth dates would
    # fabricate a spread the dataset never recorded.
    dob = date(2023, 6, 8)  # three months before the 08/09/23 weigh-in
    serial = excel_serial(dob)
    weights = ["28.5", "28.9", "27.8", "26.9", "27", "29.8", "28.9", "28.6", "31"]
    target = f"{BATCH2_DIR}/APA_Batch2_BW_IDs.xlsx"
    # numFmtId 14 is the built-in short date; the clone keeps column D centred.
    date_xf = (
        '<xf numFmtId="14" fontId="0" fillId="0" borderId="0" xfId="0" '
        'applyNumberFormat="1" applyAlignment="1"><alignment horizontal="center"/></xf>'
    )

    def apply(raw: bytes) -> bytes:
        wb = Workbook(raw)
        part = wb.sheet_part("Sheet1")
        sheet = wb.part(part)
        for offset, weight in enumerate(weights):
            row = 3 + offset
            if cell_raw_value(sheet, f"D{row}") is not None:
                raise Unimplementable(f"D{row} is not empty; spec baseline says the DOB column is")
            observed = cell_raw_value(sheet, f"C{row}")
            if observed != weight:
                raise Unimplementable(f"C{row} holds {observed!r}, spec baseline says {weight}")
            age = cell_raw_value(sheet, f"E{row}")
            if not age.startswith("-"):
                raise Unimplementable(f"E{row} is {age!r}; spec baseline says nine negative ages")

        styles = wb.part("xl/styles.xml")
        count = int(re.search(r'<cellXfs count="(\d+)">', styles).group(1))
        styles = styles.replace("</cellXfs>", date_xf + "</cellXfs>", 1)
        styles = styles.replace(f'<cellXfs count="{count}">', f'<cellXfs count="{count + 1}">', 1)
        wb.set_part("xl/styles.xml", styles)

        for offset, weight in enumerate(weights):
            row = 3 + offset
            sheet = set_number(sheet, f"D{row}", str(serial), style=str(count))
            # The formula is left in place -- that is the whole point of the
            # control -- but its cached value has to be refreshed, or a reader
            # that trusts the cache still sees the nine negative ages.
            sheet = set_cached_value(sheet, f"E{row}", repr(serial - float(weight)))
        wb.set_part(part, sheet)
        return wb.to_bytes()

    def verify(root: Path) -> list[str]:
        formulas = _open_derived(root, target)["Sheet1"]
        values = _open_derived_values(root, target)["Sheet1"]
        ages = [values[f"E{r}"].value for r in range(3, 12)]
        assert all(a > 0 for a in ages), ages
        assert str(formulas["E3"].value) == "=D3-C3", formulas["E3"].value
        assert values["D3"].value.date() == dob, values["D3"].value
        return [
            f"Sheet1!D3:D11 == {dob.isoformat()} (serial {serial}), rendered with numFmtId 14",
            f"Sheet1!E3:E11 all positive, min {min(ages)!r}",
            "the '=D3-C3' formula is untouched, so the defect is still readable from provenance",
        ]

    return Case(
        edits={target: apply},
        verify=verify,
        choices=[
            {
                "parameter": "date of birth",
                "value": dob.isoformat(),
                "why": "the spec asks for 'plausible dates' but names none; one cohort date three "
                "months before the 08/09/23 weigh-in is plausible for adult mice and invents "
                "no per-animal spread",
            },
            {
                "parameter": "cached values in E3:E11",
                "value": "recomputed as D-C",
                "why": "nothing here runs Excel, so a stale cache would leave the nine negative "
                "ages visible to every reader that trusts cached values -- the perturbation "
                "would not have happened",
            },
        ],
        caveats=[
            "The spec's expected_answer says the values are 'no longer implausible'. They are "
            "no longer *negative*, but they cannot be made plausible: C is a body weight around "
            "28 and D must be a date serial around 45,000, so '=D3-C3' necessarily yields an "
            "age near 45,000 days. The control still separates range checking from formula "
            "reading, but an agent doing a plain plausibility check on the age column will "
            "still flag it -- and would be right to. The expected_answer overstates the case."
        ],
    )


@builder("XP14-P16")
def _p16(spec: dict) -> Case:
    target = f"{BATCH2_DIR}/APA-ALL-results_MM.xlsx"
    # Row 5, not row 6. The spec said "add" but originally named Q6:S6, which
    # hold the genotype label for the three pooled columns -- writing there
    # removes a fact this control exists to preserve. Corrected 2026-09-23.
    labels = {"Q5": "B1_2-I (batch 1)", "R5": "B1_2-II (batch 1)", "S5": "B1_2-III (batch 1)"}

    def apply(raw: bytes) -> bytes:
        wb = Workbook(raw)
        part = wb.sheet_part("INDEX %-entries")
        sheet, sst = wb.part(part), wb.part("xl/sharedStrings.xml")
        for ref in labels:
            if re.search(rf'<c r="{ref}"', sheet):
                raise Unimplementable(f"{ref} is occupied; the spec requires an empty row")
        additions: dict[str, int] = {}
        for ref, text in labels.items():
            sst, index = append_shared_string(sst, text)
            additions[ref] = index
        sheet = add_shared_cells(sheet, additions)
        wb.set_part("xl/sharedStrings.xml", sst)
        wb.set_part(part, sheet)
        return wb.to_bytes()

    def verify(root: Path) -> list[str]:
        ws = _open_derived(root, target)["INDEX %-entries"]
        got = {ref: ws[ref].value for ref in labels}
        assert got == labels, got
        # The point of correcting this spec: declaring the pooling must not cost
        # the genotype label it sits above.
        for ref in ("N6", "O6", "P6", "Q6", "R6", "S6"):
            assert ws[ref].value == "dC/dC", f"{ref} lost its genotype label: {ws[ref].value!r}"
        return ["INDEX %-entries!Q5:S5 name their batch-1 origin; N6:S6 still read 'dC/dC'"]

    return Case(
        edits={target: apply},
        verify=verify,
    )


# --------------------------------------------------------------------------
# Materialisation
# --------------------------------------------------------------------------


def snapshot(root: Path) -> list[tuple[str, str]]:
    """The (relative path, sha256) pairs the dataset_id fold consumes."""
    return sorted(
        (p.relative_to(root).as_posix(), hash_file(p)) for p in root.rglob("*") if p.is_file()
    )


def _apply_renames(rel: str, renames: dict[str, str]) -> str:
    """Rename a file outright, or rewrite a directory prefix.

    XP14-P15 renames a directory. No file's bytes change, yet dataset_id must,
    because the fold serialises the path alongside the digest.
    """
    for old, new in renames.items():
        if rel == old:
            return new
        if rel.startswith(old + "/"):
            return new + rel[len(old) :]
    return rel


def materialise(case: Case, out_root: Path) -> list[tuple[str, str]]:
    """Write the derived dataset and return its (path, sha256) pairs."""
    # Every target is checked before a byte is written, so an unimplementable
    # case leaves no half-built directory behind.
    present = {p.relative_to(SRC).as_posix() for p in SRC.rglob("*") if p.is_file()}
    for rel in sorted(set(case.edits) | case.deletes):
        if rel not in present:
            raise Unimplementable(f"target absent from the snapshot: {rel}")
    for old in sorted(case.renames):
        if old not in present and not (SRC / old).is_dir():
            raise Unimplementable(f"rename source absent from the snapshot: {old}")
    for new in case.renames.values():
        if new in present:
            raise Unimplementable(f"rename would collide with an existing file: {new}")
    for rel in case.additions:
        if rel in present:
            raise Unimplementable(f"addition would overwrite an existing file: {rel}")

    if out_root.exists():
        shutil.rmtree(out_root)
    out_root.mkdir(parents=True)

    written: list[tuple[str, str]] = []
    for path in sorted(p for p in SRC.rglob("*") if p.is_file()):
        rel = path.relative_to(SRC).as_posix()
        if rel in case.deletes:
            continue
        payload = path.read_bytes()
        if rel in case.edits:
            payload = case.edits[rel](payload)
        target = out_root / _apply_renames(rel, case.renames)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        written.append((target.relative_to(out_root).as_posix(), hash_bytes(payload)))

    for rel, payload in sorted(case.additions.items()):
        target = out_root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        written.append((rel, hash_bytes(payload)))

    return sorted(written)


def gold_from_spec(spec: dict) -> dict:
    """Copy the scored expectations straight out of the YAML.

    Nothing here reads the perturbed bytes. An expectation derived from the run
    would agree with the run by construction and would test nothing.
    """
    kind = spec["kind"]
    return {
        "kind": kind,
        "category": spec["category"],
        "control_type": (
            "over_triggering_specificity_control" if kind == "remove" else "defect_injection"
        ),
        "expected_effect": {
            "operation": spec["operation"],
            "baseline": spec.get("baseline"),
            "expected_answer": spec["expected_answer"],
            "target_file": spec.get("target_file"),
            "target_cell": spec.get("target_cell") or spec.get("target_cells"),
        },
        "expected_detection": {
            "capability": spec["capability"],
            # A mechanical restatement of the vocabulary the YAML header defines:
            # inject => the agent should find it; remove => it should no longer
            # be reported, and reporting it anyway is over-triggering.
            "scoring_direction": (
                "agent_must_not_report_the_removed_defect"
                if kind == "remove"
                else "agent_must_report_the_injected_defect"
            ),
        },
    }


def build_record(
    spec: dict,
    case: Case | None,
    parent_id: str,
    derived_id: str | None,
    files: list[tuple[str, str]] | None,
    parent_files: dict[str, str],
    status: str,
    reason: str | None,
    evidence: list[str],
) -> dict:
    record: dict = {
        "schema_version": 1,
        "perturbation_id": spec["id"],
        "generator_version": GENERATOR_VERSION,
        "status": status,
        "parent_dataset_id": parent_id,
        "derived_dataset_id": derived_id,
        # No seed: every builder is a pure function of the frozen bytes and the
        # specification. Recorded as null rather than omitted so that the day a
        # stochastic perturbation is added, its absence here is conspicuous.
        "seed": None,
        "stochastic": False,
        "spec_source": {
            "path": SPEC_FILE.relative_to(REPO).as_posix(),
            "sha256": hash_file(SPEC_FILE),
        },
        "gold": gold_from_spec(spec),
    }
    if reason:
        record["not_implemented_reason"] = reason
    if case is None or files is None:
        return record

    changed, added, removed, renamed = [], [], [], []
    derived = dict(files)
    for rel, sha in parent_files.items():
        moved = _apply_renames(rel, case.renames)
        if rel in case.deletes:
            removed.append({"path": rel, "sha256": sha})
        elif moved != rel:
            renamed.append({"from": rel, "to": moved, "sha256": sha, "bytes_changed": False})
        elif derived.get(rel) != sha:
            changed.append({"path": rel, "parent_sha256": sha, "derived_sha256": derived.get(rel)})
    for rel in sorted(case.additions):
        added.append({"path": rel, "sha256": derived[rel]})

    record["applied"] = {
        "files_total": len(files),
        "files_edited": changed,
        "files_added": added,
        "files_removed": removed,
        "files_renamed": renamed,
        "bytes_preserved": case.bytes_preserved,
        "path_fold_only": case.bytes_preserved and derived_id != parent_id,
        "verification": evidence,
    }
    if case.choices:
        record["generator_choices"] = case.choices
    if case.caveats:
        record["spec_caveats"] = case.caveats
    return record


def run_suite(specs: list[dict], out_root: Path, parent_pairs: list[tuple[str, str]]) -> dict:
    """Materialise every selected case and return the index."""
    parent_id = fold_dataset_id(parent_pairs)
    parent_files = dict(parent_pairs)
    cases: list[dict] = []

    for spec in specs:
        pid = spec["id"]
        build = BUILDERS.get(pid)
        if build is None:
            cases.append(
                build_record(
                    spec,
                    None,
                    parent_id,
                    None,
                    None,
                    parent_files,
                    "not_implemented",
                    "no builder is registered for this specification",
                    [],
                )
            )
            continue
        try:
            case = build(spec)
            files = materialise(case, out_root / pid / "source")
            derived_id = fold_dataset_id(files)
            evidence = case.verify(out_root / pid / "source") if case.verify else []
            if case.bytes_preserved:
                # The point of P13 and P15: identical digests, different identity.
                if sorted(s for _, s in files) != sorted(parent_files.values()):
                    raise Unimplementable("a bytes-preserving case altered file content")
                if derived_id == parent_id:
                    raise Unimplementable("a rename left dataset_id unchanged")
                evidence.append(
                    "every file digest is unchanged, yet derived_dataset_id differs: the fold "
                    "serialises the path next to the digest"
                )
        except Unimplementable as exc:
            shutil.rmtree(out_root / pid, ignore_errors=True)
            cases.append(
                build_record(
                    spec,
                    None,
                    parent_id,
                    None,
                    None,
                    parent_files,
                    "not_implemented",
                    str(exc),
                    [],
                )
            )
            continue

        record = build_record(
            spec, case, parent_id, derived_id, files, parent_files, "applied", None, evidence
        )
        (out_root / pid / "provenance.json").write_text(
            json.dumps(record, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        cases.append(record)

    index = {
        "schema_version": 1,
        "generator_version": GENERATOR_VERSION,
        "parent_dataset_id": parent_id,
        "parent_package": PKG.relative_to(REPO).as_posix(),
        "spec_source": {
            "path": SPEC_FILE.relative_to(REPO).as_posix(),
            "sha256": hash_file(SPEC_FILE),
        },
        "applied": sum(1 for c in cases if c["status"] == "applied"),
        "not_implemented": sum(1 for c in cases if c["status"] != "applied"),
        "cases": [
            {
                "perturbation_id": c["perturbation_id"],
                "status": c["status"],
                "kind": c["gold"]["kind"],
                "control_type": c["gold"]["control_type"],
                "derived_dataset_id": c["derived_dataset_id"],
                "not_implemented_reason": c.get("not_implemented_reason"),
            }
            for c in cases
        ],
    }
    out_root.mkdir(parents=True, exist_ok=True)
    (out_root / "index.json").write_text(
        json.dumps(index, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    return index


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="derived-case root")
    parser.add_argument("--only", nargs="+", metavar="ID", help="materialise these ids only")
    parser.add_argument("--list", action="store_true", help="list the suite and exit")
    parser.add_argument(
        "--verify",
        action="store_true",
        help="materialise a second time into a temporary directory and compare every digest",
    )
    args = parser.parse_args()

    spec_doc = yaml.safe_load(SPEC_FILE.read_text(encoding="utf-8"))
    specs = spec_doc["perturbations"]
    if args.only:
        wanted = set(args.only)
        unknown = wanted - {s["id"] for s in specs}
        if unknown:
            parser.error(f"unknown perturbation id(s): {sorted(unknown)}")
        specs = [s for s in specs if s["id"] in wanted]

    if args.list:
        for spec in specs:
            mark = f"{GREEN}builder{RESET}" if spec["id"] in BUILDERS else f"{RED}none{RESET}"
            print(f"  {spec['id']}  {spec['kind']:<7} {spec['category']:<32} {mark}")
        return 0

    out = args.out.resolve()
    if out == PKG or PKG in out.parents or out in PKG.parents:
        sys.exit(
            f"{RED}refusing:{RESET} derived cases must be written outside the frozen package."
            f"\n  package: {PKG}\n  output : {out}"
        )

    before = snapshot(SRC)
    parent_id = fold_dataset_id(before)
    declared = spec_doc["base_dataset_id"]
    if parent_id != declared:
        sys.exit(
            f"{RED}refusing:{RESET} the snapshot is not the dataset these specifications "
            f"describe.\n  declared: {declared}\n  observed: {parent_id}"
        )

    print(f"parent    : {parent_id}")
    print(f"files     : {len(before)}")
    print(f"spec      : {SPEC_FILE.relative_to(REPO).as_posix()} ({len(specs)} selected)")
    print(f"output    : {out}")
    print()

    index = run_suite(specs, out, before)

    for case in index["cases"]:
        if case["status"] == "applied":
            tag = "control" if case["kind"] == "remove" else "inject "
            print(
                f"  {GREEN}+{RESET} {case['perturbation_id']}  {tag}  {case['derived_dataset_id']}"
            )
        else:
            print(f"  {RED}x{RESET} {case['perturbation_id']}  {case['not_implemented_reason']}")
    print()
    print(
        f"applied {index['applied']}/{len(index['cases'])}, "
        f"not implemented {index['not_implemented']}"
    )

    after = snapshot(SRC)
    if after != before:
        drift = sorted(set(before) ^ set(after))
        sys.exit(f"{RED}FATAL:{RESET} the frozen snapshot changed during the run: {drift}")
    print(
        f"{GREEN}parent unchanged{RESET}: {len(after)} files, dataset_id {fold_dataset_id(after)}"
    )

    if args.verify:
        scratch = Path(tempfile.mkdtemp(prefix="xp14-perturb-verify-"))
        try:
            second = run_suite(specs, scratch, before)
            mismatch = [
                (a["perturbation_id"], a["derived_dataset_id"], b["derived_dataset_id"])
                for a, b in zip(index["cases"], second["cases"], strict=True)
                if a["derived_dataset_id"] != b["derived_dataset_id"] or a["status"] != b["status"]
            ]
            if mismatch:
                sys.exit(f"{RED}NOT DETERMINISTIC:{RESET} {mismatch}")
            # dataset_id folds file digests, so equal ids already imply equal
            # bytes; the per-file comparison names the culprit when they differ.
            for pid in (c["perturbation_id"] for c in index["cases"] if c["status"] == "applied"):
                first_files = snapshot(out / pid / "source")
                again = snapshot(scratch / pid / "source")
                if first_files != again:
                    sys.exit(f"{RED}NOT DETERMINISTIC:{RESET} {pid} differs file by file")
            print(f"{GREEN}deterministic{RESET}: two independent runs agree on every digest")
            if snapshot(SRC) != before:
                sys.exit(f"{RED}FATAL:{RESET} the snapshot changed during verification")
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    return 0 if index["not_implemented"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
