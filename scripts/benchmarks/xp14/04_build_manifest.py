"""Write the XP14 benchmark package manifest.

This is the PACKAGE manifest (what the benchmark is), distinct from the
data2agent ingest manifest under ingest/ (what the dataset is). It carries the
file-class taxonomy, which is a benchmark-level judgement about the role each
artifact plays, not something derivable from bytes.
"""

from __future__ import annotations

import collections
import json
from pathlib import Path

import yaml


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
PKG = REPO / "benchmarks" / "xp14_apa"

entries = json.loads((PKG / "_entries.tmp.json").read_text(encoding="utf-8"))

# Derived, never hard-coded. An earlier version asserted a statement count and a
# list of pending questions as literals; both went stale the moment the
# adjudication was answered and the statements regenerated, leaving the manifest
# contradicting the gold tables it describes.
_decisions = yaml.safe_load((PKG / "adjudication" / "decisions.yaml").read_text(encoding="utf-8"))
OPEN_QUESTIONS = [q["id"] for q in _decisions["questions"] if q.get("status") != "answered"]
ANSWERED_QUESTIONS = [q["id"] for q in _decisions["questions"] if q.get("status") == "answered"]
_statements_path = PKG / "gold" / "semantic_statements.jsonl"
STATEMENT_COUNT = (
    sum(1 for line in _statements_path.read_text(encoding="utf-8").splitlines() if line.strip())
    if _statements_path.exists()
    else 0
)
checks = json.loads((PKG / "checksums.json").read_text(encoding="utf-8"))

# extension-vs-signature conflicts, computed rather than asserted
OOXML_EXT = {"xlsx", "pptx", "docx"}
conflicts = []
for e in entries:
    ext, fmt = e["extension"], str(e["detected_format"] or "")
    if ext == "xls" and "zip" in fmt.lower():
        conflicts.append(
            {
                "path": e["path"],
                "extension_claims": "xls (OLE2/BIFF)",
                "bytes_indicate": "ZIP/OOXML",
                "signature": "50 4B 03 04",
            }
        )

classes = collections.Counter(e["file_class"] for e in entries)

anomaly_ids = [f"XP14-A{i:03d}" for i in range(1, 13)]

manifest = {
    # 0.x for the same reason the manifest schema is: this package is not
    # finished. Four questions remain open and no artifact is cleared for
    # release.
    "package_version": "0.1.0-draft",
    "benchmark_id": "xp14_apa",
    "title": "XP14 APA — Shank3 active place avoidance",
    # Derived, like the question lists below. A hard-coded status line is the
    # same staleness bug in prose form.
    "status": (
        "frozen; gold standard draft; "
        f"{len(ANSWERED_QUESTIONS)} owner question(s) answered, "
        f"{len(OPEN_QUESTIONS)} open"
    ),
    "backlog_item": "D2A-17",
    "dataset": {
        "dataset_id": checks["dataset_id"],
        "dataset_id_method": checks["dataset_id_method"],
        "file_count": checks["file_count"],
        "total_bytes": checks["total_bytes"],
        "source_path": "source/",
        "immutable": True,
        "repair_policy": "none — incorrect values and contradictory metadata are preserved verbatim",
    },
    "provenance_of_snapshot": {
        "origin": "Shank3-experiments/Data2Agent_candidates/XP14_APA (w. A. Besnard)",
        "copied_at": "2026-09-22",
        "verified_byte_identical": True,
        "verification_method": "recursive diff against the origin tree at copy time",
    },
    "file_classes": {
        "rationale": (
            "XP14 shows that experimental metadata does not live only in CSV/Excel/JSON. "
            "Rotation speed, chance level and an analysis-affecting exclusion rule exist "
            "only in presentation artifacts. Treating those as optional prose attachments "
            "would discard scientifically essential information, so role is modelled "
            "explicitly and is assigned by observed function, not by extension."
        ),
        "vocabulary": {
            "data": "primary measurements as exported by the acquisition software",
            "metadata": "descriptors of the animals or the study",
            "protocol_evidence": "procedure and protocol records (none in XP14; present in XP4)",
            "analysis_output": "derived tables, computed indices, statistical projects",
            "presentation_artifact": "slides and reports; in XP14 a load-bearing metadata carrier",
        },
        "counts": dict(sorted(classes.items())),
    },
    "format_findings": {
        "extension_signature_conflicts": len(conflicts),
        "conflicts": conflicts,
        "note": (
            "data2agent v0.1 records format_id 'zip-container' detected_by 'signature' for "
            "these files, which is correct, but emits no warning that the extension "
            "disagreed. The conflict is therefore absent from the ingest manifest. "
            "See gold/anomalies.yaml XP14-A001 and the D2A-46 backlog item (issue #17)."
        ),
    },
    "gold_standard": {
        "path": "gold/",
        "files": {
            "animals.csv": "15 animals; identity, sex, genotype, body weight, analysis inclusion",
            "identifier_crosswalk.csv": "the 7 identifier systems and their collisions",
            "sessions.csv": "75 sessions; protocol, timing, shock zone",
            "relationships.csv": "12 table relationships with coverage and status",
            "provenance_edges.csv": "15 transformation edges, classified by reproducibility",
            "anomalies.yaml": "12 natural anomalies with expected agent actions",
            "fair_expected.json": "expected FAIR verdicts, and where v0.1 diverges",
            "semantic_statements.jsonl": (f"{STATEMENT_COUNT} statements with layered evidence"),
        },
        "answered_owner_questions": ANSWERED_QUESTIONS,
        "pending_owner_questions": OPEN_QUESTIONS,
        "pending_marker": (
            "PENDING-A<n> appears in any gold field a question still bears on; "
            "an answered question is reflected in the gold tables instead"
        ),
    },
    "anomalies": {
        "count": len(anomaly_ids),
        "ids": anomaly_ids,
        "repair_allowed_anywhere": False,
        "expected_actions": ["report", "flag", "abstain_and_report_conflict", "verify", "discover"],
    },
    "perturbations": {
        "path": "perturbations/perturbations.yaml",
        "applied": False,
        "inject_count": 12,
        "remove_count": 4,
        "note": "removal items are over-triggering controls and are scored as strictly as injections",
    },
    "known_tool_gaps": [
        {
            "id": "D2A-46",
            "issue": 17,
            "summary": "extension/signature disagreement is not surfaced",
            "impact": "20 OOXML files named .xls escaped I1-DATA-FORMATS-OPEN, which flagged only the 9 files named .xlsx",
        },
        {
            "id": "D2A-47",
            "issue": 18,
            "summary": "the tabular profiler reads only delimited text, so OOXML workbooks yield 0 tables",
            "impact": "R1.3-MISSING-VALUES-DECLARED returned not_applicable on a dataset with substantial undeclared missingness",
        },
        {
            "id": "D2A-48",
            "summary": ".pzfx (GraphPad XML) is unrecognised",
            "impact": "one warning; format stays unknown, which is contract-correct but improvable",
        },
        {
            "id": "D2A-49a",
            "summary": "metadata recognition is by filename convention only",
            "impact": "the registry workbook and the presentations carry real metadata and are invisible to F2/F3/F4/I2/R1.2",
        },
    ],
}

(PKG / "manifest.json").write_text(
    json.dumps(manifest, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
)
(PKG / "_entries.tmp.json").unlink()

print("manifest.json written")
print("  dataset_id:", manifest["dataset"]["dataset_id"])
print("  file classes:", manifest["file_classes"]["counts"])
print("  extension/signature conflicts:", len(conflicts))
print("  known tool gaps:", [g["id"] for g in manifest["known_tool_gaps"]])
