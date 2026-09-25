"""Freeze a source tree into a benchmark package: byte-for-byte copy + checksums.

Public and data-free: every path comes from the command line, nothing about the
dataset is named here.

    python scripts/benchmarks/xp16/01_freeze.py --source <origin-dir> --package <pkg-dir>

Writes ``<pkg>/source/`` (an exact copy of ``<origin-dir>``) and
``<pkg>/checksums.json`` (sha256 per file, total bytes, and the Data2Agent
``dataset_id`` fold). The copy is verified file by file, and the origin tree is
digested before and after the copy so a run that disturbed it aborts instead of
producing a package that silently differs from what the owner holds.

Re-running against an existing package is allowed only when the bytes already
there are identical; the script refuses to overwrite a drifted snapshot, because
a frozen benchmark that can be quietly refrozen is not frozen.

The dataset_id is computed with the repository's own fold
(``data2agent.ingest.checksum.dataset_id``) so it equals what ``data2agent
ingest`` reports for the same tree. That is the one use of the package here and
it only hashes; the gold standard itself never goes through data2agent.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

from xp16lib import REPO, sha256_file

sys.path.insert(0, str(REPO / "src"))

from data2agent.ingest.checksum import dataset_id as fold_dataset_id  # noqa: E402


def digest_tree(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): sha256_file(p)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--source", type=Path, required=True, help="origin tree to freeze")
    parser.add_argument("--package", type=Path, required=True, help="benchmark package dir")
    args = parser.parse_args()

    origin = args.source.resolve()
    pkg = args.package.resolve()
    if not origin.is_dir():
        sys.exit(f"source is not a directory: {origin}")
    target = pkg / "source"

    before = digest_tree(origin)
    if not before:
        sys.exit("source tree holds no files")

    if target.exists():
        existing = digest_tree(target)
        if existing != before:
            sys.exit(
                f"{target} already exists and differs from the origin; refusing to refreeze.\n"
                "Move it aside deliberately if the origin really changed."
            )
        print("source/ already frozen and identical; recomputing checksums only")
    else:
        target.mkdir(parents=True)
        for rel in before:
            dst = target / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(origin / rel, dst)  # bytes only; no metadata games
            shutil.copystat(origin / rel, dst)

    copied = digest_tree(target)
    after = digest_tree(origin)
    if after != before:
        sys.exit("the origin tree changed while it was being frozen; aborting")
    if copied != before:
        bad = sorted(k for k in before if copied.get(k) != before[k])
        sys.exit(f"copy mismatch for {len(bad)} file(s): {bad}")

    pairs = sorted(copied.items())
    did = fold_dataset_id(pairs)
    files = [
        {"path": rel, "sha256": sha, "bytes": (target / rel).stat().st_size} for rel, sha in pairs
    ]
    checksums = {
        "algorithm": "sha256",
        "dataset_id": did,
        "dataset_id_method": (
            "sha256 fold over sorted (relative_path, sha256) pairs, each serialised "
            "as path + '\\n' + sha256 + '\\n'; data2agent.ingest.checksum.dataset_id"
        ),
        "file_count": len(files),
        "total_bytes": sum(f["bytes"] for f in files),
        "files": files,
    }
    (pkg / "checksums.json").write_text(
        json.dumps(checksums, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print("dataset_id:", did)
    print("files:", len(files), " total bytes:", checksums["total_bytes"])
    print("origin unchanged; copy verified byte-for-byte")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
