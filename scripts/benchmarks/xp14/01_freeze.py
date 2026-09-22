"""Freeze the XP14 snapshot: checksums, dataset_id, and the file-class manifest.

Reuses the repository's own checksum fold and format detection so the
benchmark package's dataset_id is the same value `data2agent ingest` produces.
Read-only with respect to the snapshot.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _repo_root() -> Path:
    """Repository root, derived from this file's location.

    Keeps the generators runnable on any machine and inside CI. Override with
    the D2A_REPO environment variable if the scripts are vendored elsewhere.
    """
    import os

    env = os.environ.get("D2A_REPO")
    if env:
        return Path(env).resolve()
    return Path(__file__).resolve().parents[3]


REPO = _repo_root()
sys.path.insert(0, str(REPO / "src"))

from data2agent.ingest.checksum import dataset_id as fold_dataset_id  # noqa: E402
from data2agent.ingest.checksum import hash_file  # noqa: E402
from data2agent.ingest.formats import detect  # noqa: E402

PKG = REPO / "benchmarks" / "xp14_apa"
SRC = PKG / "source"

# The five-way file-class taxonomy. Assignment is by observed role, established
# in the forensic audit -- not by extension, which XP14 shows to be unreliable.
DATA = "data"
METADATA = "metadata"
PROTOCOL_EVIDENCE = "protocol_evidence"
ANALYSIS_OUTPUT = "analysis_output"
PRESENTATION = "presentation_artifact"

CLASS_RULES: list[tuple[str, str, str]] = [
    # (path substring, class, why)
    ("IDs Batch Sex Group.xlsx", METADATA, "animal registry: batch, local id, genotype, sex"),
    ("APA_Batch2_BW_IDs.xlsx", METADATA, "batch-2 biological id, body weight, DOB column"),
    ("_MM.xls", ANALYSIS_OUTPUT, "hand-annotated derivative of a raw session export"),
    ("_results_HD.xlsx", ANALYSIS_OUTPUT, "reshaped session results"),
    ("APA-ALL-results", ANALYSIS_OUTPUT, "consolidated results, pivots and computed indices"),
    (".prism", ANALYSIS_OUTPUT, "GraphPad project (zip of JSON)"),
    (".pzfx", ANALYSIS_OUTPUT, "GraphPad XML project"),
    (
        ".pptx",
        PRESENTATION,
        "result presentation; sole carrier of apparatus parameters and the exclusion rule",
    ),
]


def classify(rel: str) -> tuple[str, str]:
    for needle, cls, why in CLASS_RULES:
        if needle in rel:
            return cls, why
    if rel.endswith(".xls"):
        return DATA, "raw session export from the acquisition software"
    return "unknown", "no rule matched"


def main() -> None:
    files = sorted(p for p in SRC.rglob("*") if p.is_file())
    entries: list[dict[str, object]] = []
    pairs: list[tuple[str, str]] = []

    for p in files:
        rel = p.relative_to(SRC).as_posix()
        sha = hash_file(p)
        pairs.append((rel, sha))
        info, warnings = detect(p)
        cls, why = classify(rel)
        ext = p.suffix.lower().lstrip(".")
        actual = getattr(info, "format", None) or getattr(info, "name", None) or str(info)
        detected_by = getattr(info, "detected_by", None)
        entries.append(
            {
                "path": rel,
                "bytes": p.stat().st_size,
                "sha256": sha,
                "extension": ext,
                "detected_format": actual,
                "detected_by": detected_by,
                "file_class": cls,
                "class_rationale": why,
                "format_warnings": warnings,
            }
        )

    did = fold_dataset_id(pairs)

    checksums = {
        "algorithm": "sha256",
        "dataset_id": did,
        "dataset_id_method": (
            "sha256 fold over sorted (relative_path, sha256) pairs, each serialised "
            "as path + '\\n' + sha256 + '\\n'; see docs/data-contract.md"
        ),
        "file_count": len(entries),
        "total_bytes": sum(int(e["bytes"]) for e in entries),
        "files": [{"path": e["path"], "sha256": e["sha256"], "bytes": e["bytes"]} for e in entries],
    }
    (PKG / "checksums.json").write_text(
        json.dumps(checksums, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    print("dataset_id:", did)
    print("files:", len(entries), "total bytes:", checksums["total_bytes"])
    print()
    print("=== extension vs detected format ===")
    mismatch = 0
    for e in entries:
        fmt = str(e["detected_format"])
        if e["extension"] == "xls" and "xls" not in fmt.lower():
            mismatch += 1
        print(
            f"  {e['path'][:70]:<70} ext={e['extension']:<5} detected={fmt:<28} by={e['detected_by']}"
        )
    print()
    print("=== file classes ===")
    from collections import Counter

    for cls, n in sorted(Counter(str(e["file_class"]) for e in entries).items()):
        print(f"  {cls:<24} {n}")

    # stash the classified entries for the manifest step
    (PKG / "_entries.tmp.json").write_text(
        json.dumps(entries, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
