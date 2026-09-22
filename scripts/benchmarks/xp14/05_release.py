"""Validate and materialise the public XP14 benchmark package.

Implements the allowlist publication model from issue #19.

Two guarantees, both enforced rather than documented:

1. The private package is never modified. This script opens
   benchmarks/xp14_apa/ read-only and writes exclusively to a staging
   directory that must sit outside it.
2. Nothing is published that is not explicitly cleared. The default is deny,
   every clearance gate must be true, and every leak check must pass. A leak
   finding blocks materialisation; it is not a warning.

Usage
    python scripts/benchmarks/xp14/05_release.py --check
    python scripts/benchmarks/xp14/05_release.py --out ./xp14-public [--force]

--check reports the decision for every artifact and runs the leak checks
without writing anything. It is the useful mode while clearance is pending.
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import shutil
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    sys.exit("pyyaml is required: pip install pyyaml")


def _repo_root() -> Path:
    env = os.environ.get("D2A_REPO")
    return Path(env).resolve() if env else Path(__file__).resolve().parents[3]


REPO = _repo_root()
PKG = REPO / "benchmarks" / "xp14_apa"
MANIFEST = Path(__file__).resolve().parent / "release.yaml"

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"


def load_manifest() -> dict:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


def gates_pass(man: dict) -> tuple[bool, list[str]]:
    """Every clearance gate must be explicitly true."""
    failed = [
        k
        for k, v in man["clearance"].items()
        if k not in {"cleared_by", "cleared_at", "notes"} and v is not True
    ]
    return not failed, failed


def decide(rel: str, man: dict) -> dict | None:
    """First matching rule wins; absence means deny."""
    for entry in man["artifacts"]:
        if fnmatch.fnmatch(rel, entry["path"]) or rel.startswith(entry["path"].rstrip("*")):
            return entry
    return None


def scan(path: Path, checks: list[dict]) -> list[tuple[str, int, str]]:
    """Return (check_id, line_no, excerpt) for every leak finding."""
    findings: list[tuple[str, int, str]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return findings  # binary: nothing textual to leak
    for check in checks:
        rx = re.compile(check["pattern"])
        for n, line in enumerate(text.splitlines(), 1):
            m = rx.search(line)
            if m:
                findings.append((check["id"], n, m.group(0)))
                break  # one exemplar per check per file is enough to block
    return findings


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--check", action="store_true", help="report only; write nothing")
    ap.add_argument("--out", type=Path, help="staging directory; must be outside the package")
    ap.add_argument(
        "--force",
        action="store_true",
        help="materialise despite leak findings; refuses if clearance gates fail",
    )
    args = ap.parse_args()

    if not args.check and not args.out:
        ap.error("pass --check, or --out to materialise")

    man = load_manifest()
    checks = man.get("leak_checks", [])

    if args.out:
        out = args.out.resolve()
        if out == PKG or PKG in out.parents or out in PKG.parents:
            sys.exit(
                f"{RED}refusing:{RESET} staging directory must sit outside the private "
                f"package.\n  package: {PKG}\n  staging: {out}"
            )

    ok, failed = gates_pass(man)
    print(f"benchmark : {man['benchmark_id']}")
    print(f"dataset_id: {man['dataset_id']}")
    print(f"clearance : {GREEN + 'all gates pass' + RESET if ok else RED + 'BLOCKED' + RESET}")
    for f in failed:
        print(f"            {RED}x{RESET} {f}")
    print()

    files = sorted(p for p in PKG.rglob("*") if p.is_file())
    tally: dict[str, int] = {}
    cleared: list[tuple[Path, str, dict]] = []
    blocked: list[tuple[str, list]] = []

    for p in files:
        rel = p.relative_to(PKG).as_posix()
        entry = decide(rel, man)
        decision = entry["decision"] if entry else "deny (not listed)"
        tally[decision] = tally.get(decision, 0) + 1
        if decision != "cleared":
            continue
        findings = scan(p, checks)
        if findings:
            blocked.append((rel, findings))
        else:
            cleared.append((p, rel, entry))

    print("decisions")
    for d, n in sorted(tally.items()):
        colour = GREEN if d == "cleared" else (YELLOW if d == "pending_review" else DIM)
        print(f"  {colour}{d:<22}{RESET} {n}")
    print()

    # Leak checks run on pending_review artifacts too, so the owner review has
    # the findings in front of it rather than discovering them at release time.
    print("leak scan (pending_review artifacts, advisory)")
    advisory = 0
    for p in files:
        rel = p.relative_to(PKG).as_posix()
        entry = decide(rel, man)
        if not entry or entry["decision"] != "pending_review":
            continue
        findings = scan(p, checks)
        if findings:
            advisory += 1
            ids = ", ".join(sorted({f[0] for f in findings}))
            declared = set(entry.get("transforms", []))
            need = {
                "facility_identifier": "pseudonymise_animal_ids",
                "design_encoding_path": "redact_source_paths",
                "personal_name": "redact_personal_names",
            }
            missing = {
                need[i] for i in {f[0] for f in findings} if need.get(i) and need[i] not in declared
            }
            flag = (
                f"  {RED}<- no transform declared: {', '.join(sorted(missing))}{RESET}"
                if missing
                else ""
            )
            print(f"  {YELLOW}!{RESET} {rel:<42} {ids}{flag}")
    if not advisory:
        print(f"  {GREEN}none{RESET}")
    print()

    if blocked:
        print(f"{RED}blocked{RESET} — cleared artifacts carrying leak findings")
        for rel, findings in blocked:
            for cid, line, excerpt in findings:
                print(f"  {rel}:{line}  {cid}  {excerpt!r}")
        print()

    if args.check:
        print(f"{DIM}--check: nothing written{RESET}")
        return 1 if blocked else 0

    if not ok:
        sys.exit(f"{RED}refusing to materialise:{RESET} clearance gates not satisfied")
    if blocked and not args.force:
        sys.exit(f"{RED}refusing to materialise:{RESET} leak findings in cleared artifacts")
    if not cleared:
        sys.exit(
            f"{YELLOW}nothing to materialise:{RESET} no artifact has decision 'cleared'. "
            "This is the expected state while XP14 is under private adjudication."
        )

    out.mkdir(parents=True, exist_ok=True)
    for src, rel, entry in cleared:
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        t = entry.get("transforms")
        note = f"  (declared transforms: {', '.join(t)} — NOT YET IMPLEMENTED)" if t else ""
        print(f"  {GREEN}+{RESET} {rel}{note}")
    print(f"\nmaterialised {len(cleared)} artifact(s) to {out}")
    print(f"{DIM}private package untouched: {PKG}{RESET}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
