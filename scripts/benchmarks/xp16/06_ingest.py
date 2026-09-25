"""Ingest the frozen snapshot under a declared benchmark condition, reproducibly.

Public and data-free. The condition is read from the package:

    <pkg>/config/conditions.json
    {"mode": "structured",
     "conditions": {
        "undeclared": {"output": "ingest_undeclared"},
        "declared":   {"output": "ingest_declared",
                       "layout": "layouts.json",               # ingest --layout
                       "relationships": "relationships.json",  # relationships --declarations
                       "crosswalks": {"<name>": "crosswalk.csv"},  # --crosswalk NAME=PATH
                       "transform_crosswalks": {"<gold transform>": "<name>"}}}}

(paths relative to ``<pkg>/config``), and run as

    python scripts/benchmarks/xp16/06_ingest.py --package <pkg> --condition declared

It runs ``data2agent ingest`` (with ``--layout`` when declared) and then
``data2agent relationships`` (with ``--declarations`` / ``--crosswalk`` when
declared), then writes ``<output>/condition.json``: the condition name, the
sha256 of every declaration file, the dataset_id, the data2agent version and
git commit, and the exact commands. ``05_baseline.py`` and ``04_run_agent.py``
copy that record into their results, so every score is tied to one exact
configuration. A condition whose declared layout sha256 is not the one the
manifest recorded is refused, and so is an ingest of a snapshot that no longer
matches ``checksums.json``.

Why conditions matter: once the layout is *declared*, finding the header rows is
no longer something the tool (or the agent) is measured on -- the benchmark then
measures retrieval and computation given the right tables. The undeclared
condition keeps measuring discovery. Report which one a number comes from.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from xp16lib import REPO, sha256_file


def _git_commit() -> str | None:
    try:
        done = subprocess.run(  # noqa: S603, S607
            ["git", "-C", str(REPO), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=30,
        )
        return done.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        return None


def _run(command: list[str]) -> None:
    env_path = str(REPO / "src")
    import os  # noqa: PLC0415

    env = {**os.environ, "PYTHONPATH": env_path + os.pathsep + os.environ.get("PYTHONPATH", "")}
    done = subprocess.run(command, capture_output=True, text=True, env=env)  # noqa: S603
    if done.returncode != 0:
        sys.exit(f"command failed ({done.returncode}): {command}\n{done.stdout}\n{done.stderr}")


def verify_snapshot(pkg: Path) -> dict[str, Any]:
    checksums = json.loads((pkg / "checksums.json").read_text(encoding="utf-8"))
    src = pkg / "source"
    bad = [
        f["path"]
        for f in checksums["files"]
        if not (src / f["path"]).is_file() or sha256_file(src / f["path"]) != f["sha256"]
    ]
    if bad:
        sys.exit(f"snapshot drift, refusing to ingest: {bad}")
    return checksums


def ingest(pkg: Path, name: str, *, force: bool = False) -> Path:
    config_dir = pkg / "config"
    conditions = json.loads((config_dir / "conditions.json").read_text(encoding="utf-8"))
    if name not in conditions["conditions"]:
        sys.exit(f"unknown condition {name!r}; known: {sorted(conditions['conditions'])}")
    cond = conditions["conditions"][name]
    mode = conditions.get("mode", "structured")
    checksums = verify_snapshot(pkg)
    out = pkg / cond["output"]
    if out.exists():
        if not force:
            sys.exit(f"{out} exists; pass --force to rebuild it")
        shutil.rmtree(out)

    declarations: dict[str, Any] = {}
    ingest_cmd = [
        sys.executable, "-m", "data2agent.cli", "ingest", str(pkg / "source"),
        "-o", str(out), "--mode", mode,
    ]  # fmt: skip
    if cond.get("layout"):
        layout = config_dir / cond["layout"]
        ingest_cmd += ["--layout", str(layout)]
        declarations["layout"] = {"file": cond["layout"], "sha256": sha256_file(layout)}
    rel_cmd = [sys.executable, "-m", "data2agent.cli", "relationships", str(out)]
    if cond.get("relationships"):
        rel = config_dir / cond["relationships"]
        rel_cmd += ["--declarations", str(rel)]
        declarations["relationships"] = {"file": cond["relationships"], "sha256": sha256_file(rel)}
    for cw_name, cw_file in sorted((cond.get("crosswalks") or {}).items()):
        cw = config_dir / cw_file
        rel_cmd += ["--crosswalk", f"{cw_name}={cw}"]
        declarations.setdefault("crosswalks", {})[cw_name] = {
            "file": cw_file,
            "sha256": sha256_file(cw),
        }

    _run(ingest_cmd)
    _run(rel_cmd)

    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("dataset_id") != checksums["dataset_id"]:
        sys.exit("ingest dataset_id differs from checksums.json")
    recorded = (manifest.get("layout_declaration") or {}).get("sha256")
    if "layout" in declarations and recorded != declarations["layout"]["sha256"]:
        sys.exit(f"manifest records layout sha256 {recorded}, not the declared file's")
    if "layout" not in declarations and recorded:
        sys.exit("undeclared condition, but the manifest records a layout declaration")

    import data2agent  # noqa: PLC0415

    record = {
        "condition": name,
        "dataset_id": checksums["dataset_id"],
        "mode": mode,
        "declarations": declarations,
        # which declared crosswalk realises which gold identifier transform, so the
        # baseline can hand a cross-file aggregation to aggregate_join
        "transform_crosswalks": dict(cond.get("transform_crosswalks") or {}),
        "manifest_sha256": sha256_file(out / "manifest.json"),
        "relationships_sha256": (
            sha256_file(out / "relationships.json")
            if (out / "relationships.json").exists()
            else None
        ),
        "data2agent_version": getattr(data2agent, "__version__", None),
        "data2agent_commit": _git_commit(),
        "commands": [
            [str(c).replace(str(pkg), "<pkg>") for c in ingest_cmd],
            [str(c).replace(str(pkg), "<pkg>") for c in rel_cmd],
        ],
    }
    (out / "condition.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--condition", required=True, help="a key of config/conditions.json")
    parser.add_argument("--force", action="store_true", help="rebuild an existing output dir")
    args = parser.parse_args()
    sys.path.insert(0, str(REPO / "src"))
    out = ingest(args.package.resolve(), args.condition, force=args.force)
    record = json.loads((out / "condition.json").read_text(encoding="utf-8"))
    print(
        json.dumps(
            {k: record[k] for k in ("condition", "declarations", "data2agent_commit")}, indent=2
        )
    )
    print("ingested to", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
