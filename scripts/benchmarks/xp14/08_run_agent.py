"""Run one benchmark cell: an agent assessing XP14 through Data2MCP.

Public: contains the prompt and the harness, never dataset content. What it
writes is an assessment in the same shape `07_score.py` already scores, so an
agent's answer and the deterministic profile's answer are compared by one
scorer rather than two.

A cell is `harness x orchestrator x model x constraint`. This script fixes
orchestrator = the host's own agent loop and varies the rest through flags.

**The MCP surface is the independent variable, so it is isolated.** The server
is passed per-run with `--mcp-config` and `--strict-mcp-config`, never
registered globally. Without the strict flag the host also loads whatever the
operator happens to have configured, and a `raw` condition with three unrelated
servers attached is not a raw condition. This also leaves the operator's own
configuration untouched, which a benchmark has no business editing.

**The prompt is identical in every mode.** It lives in `prompt_fair.md` beside
this script so it is reviewable as the instrument it is, and it names the twelve
rule ids because a verdict that cannot be lined up against the gold cannot be
scored at all. It deliberately does NOT carry the rules' operational
definitions: supplying those would erase the difference between `structured`
and `fair-rules`, where reading the canonical registry is precisely what the
mode adds. It also says nothing about which indicators ought to be `unknown` --
that is the measurement, not the setup.

Usage
    python scripts/benchmarks/xp14/08_run_agent.py \
        --ingest <ingest-dir> --mode structured --model claude-sonnet-5 \
        --out <run-dir>

Then score it:
    python scripts/benchmarks/xp14/07_score.py <run-dir>/assessment.json
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_VERDICTS = frozenset({"pass", "fail", "unknown", "not_applicable"})

# Tools each mode exposes, mirrored from `data2agent modes`. Passed to the host
# as an allowlist so a run cannot reach past its own condition even if the
# server offers more than expected.
_TOOL_PREFIX = "mcp__data2agent__"


def _repo_root() -> Path:
    env = os.environ.get("D2A_REPO")
    return Path(env).resolve() if env else Path(__file__).resolve().parents[3]


REPO = _repo_root()
PROMPT = Path(__file__).resolve().parent / "prompt_fair.md"


def mode_tools(mode: str) -> list[str]:
    """The tool names a mode exposes, asked of the library rather than listed.

    Hard-coding them here would let this script and the server disagree about
    what a condition IS, and the condition is the thing being varied.
    """
    sys.path.insert(0, str(REPO / "src"))
    from data2agent.mcp.modes import MODES  # noqa: PLC0415

    if mode not in MODES:
        sys.exit(f"unknown mode {mode!r}; known: {', '.join(sorted(MODES))}")
    definition = MODES[mode]
    tools = getattr(definition, "tools", None)
    if not tools:
        sys.exit(f"mode {mode!r} exposes no tools")
    return sorted(tools)


def write_mcp_config(path: Path, ingest: Path, mode: str) -> Path:
    """An ephemeral host config naming exactly one server."""
    python = os.environ.get("D2A_PYTHON") or sys.executable
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "data2agent": {
                        "command": python,
                        "args": [
                            "-m",
                            "data2agent.cli",
                            "serve",
                            str(ingest),
                            "--mode",
                            mode,
                        ],
                        "env": {},
                    }
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def extract_json(text: str) -> dict[str, Any] | None:
    """Recover the JSON object from a reply that may not be only JSON.

    The prompt asks for bare JSON. Models comply unevenly, and a run lost to a
    stray code fence is a measurement thrown away for no reason. A reply that
    cannot be parsed at all is recorded as such rather than guessed at -- the
    failure is a real property of the cell.
    """
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if 0 <= start < end:
        try:
            return json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            pass
    return None


def normalise(payload: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    """Coerce the agent's answer into assessment shape, reporting what was wrong.

    Nothing is repaired silently. A verdict outside the vocabulary is dropped
    and named, because a rule the agent answered unintelligibly is not the same
    as a rule it answered correctly, and the scorer must not be handed a guess.
    """
    problems: list[str] = []
    results = payload.get("results")
    if not isinstance(results, list):
        return [], ["reply has no 'results' list"]

    seen: set[str] = set()
    rows: list[dict[str, Any]] = []
    for item in results:
        if not isinstance(item, dict):
            problems.append(f"result entry is not an object: {item!r:.60}")
            continue
        rule_id = item.get("rule_id")
        verdict = item.get("result")
        if not isinstance(rule_id, str):
            problems.append(f"result entry has no rule_id: {item!r:.60}")
            continue
        if rule_id in seen:
            problems.append(f"{rule_id} answered more than once; keeping the first")
            continue
        if verdict not in _VERDICTS:
            problems.append(f"{rule_id} returned {verdict!r}, which is not a verdict")
            continue
        seen.add(rule_id)
        rows.append(
            {
                "rule_id": rule_id,
                "principle": rule_id.split("-", 1)[0],
                "result": verdict,
                "evidence": [],
                "rationale": str(item.get("rationale", ""))[:1000],
                "inferred": True,
            }
        )
    return rows, problems


def run_cell(args: argparse.Namespace) -> int:
    if not PROMPT.exists():
        sys.exit(f"prompt not found: {PROMPT}")
    ingest = args.ingest.resolve()
    if not (ingest / "manifest.json").exists():
        sys.exit(f"not an ingest directory (no manifest.json): {ingest}")
    host = shutil.which(args.host)
    if host is None:
        sys.exit(f"host not on PATH: {args.host}")

    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    config = write_mcp_config(out / "mcp-config.json", ingest, args.mode)
    tools = mode_tools(args.mode)
    allowed = ",".join(f"{_TOOL_PREFIX}{name}" for name in tools)

    command = [
        host,
        "-p",
        PROMPT.read_text(encoding="utf-8"),
        "--mcp-config",
        str(config),
        "--strict-mcp-config",
        "--allowed-tools",
        allowed,
        "--output-format",
        "json",
    ]
    if args.model:
        command += ["--model", args.model]
    if args.permission_mode:
        command += ["--permission-mode", args.permission_mode]

    started = time.time()
    completed = subprocess.run(  # noqa: S603
        command,
        capture_output=True,
        text=True,
        timeout=args.timeout,
        stdin=subprocess.DEVNULL,
        cwd=str(out),
    )
    elapsed = round(time.time() - started, 1)

    raw = out / "host-response.json"
    raw.write_text(completed.stdout or "", encoding="utf-8")
    if completed.returncode != 0:
        print(f"  host exited {completed.returncode}")
        print((completed.stderr or "")[:2000])
        return 1

    try:
        envelope = json.loads(completed.stdout)
    except json.JSONDecodeError:
        print(f"  host did not return JSON; raw reply kept at {raw}")
        return 1

    reply = envelope.get("result", "")
    payload = extract_json(reply if isinstance(reply, str) else json.dumps(reply))
    if payload is None:
        print(f"  the agent's reply was not JSON; kept at {raw}")
        (out / "agent-reply.txt").write_text(str(reply), encoding="utf-8")
        return 1

    rows, problems = normalise(payload)

    # The generator block is the run record. It carries every axis of the
    # benchmark contract that this script knows, so two assessments can be
    # compared or refused. It does not yet carry the data2agent build -- see
    # issue #33; when that lands, it belongs here too.
    assessment = {
        "assessment_version": "0.1.0",
        "dataset_id": json.loads((ingest / "manifest.json").read_text(encoding="utf-8")).get(
            "dataset_id"
        ),
        "profile": {"id": "fair", "version": "0.1.0"},
        "generator": {
            "mode": args.mode,
            "orchestrator": f"{args.host}-agent-loop",
            "host": f"{args.host}/{_host_version(host)}",
            "model": args.model or _reported_model(envelope) or "host-default",
            "prompt_sha256": _sha256(PROMPT.read_bytes()),
            "ran_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "results": rows,
    }
    (out / "assessment.json").write_text(
        json.dumps(assessment, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    (out / "run.json").write_text(
        json.dumps(
            {
                "mode": args.mode,
                "tools_allowed": tools,
                "elapsed_seconds": elapsed,
                "cost_usd": envelope.get("total_cost_usd"),
                "usage": envelope.get("usage"),
                "permission_denials": envelope.get("permission_denials"),
                "stop_reason": envelope.get("stop_reason"),
                "answered": len(rows),
                "problems": problems,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    print(f"  mode       : {args.mode}  ({len(tools)} tools)")
    print(f"  model      : {assessment['generator']['model']}")
    print(f"  answered   : {len(rows)}/12 indicator(s) in {elapsed}s")
    if envelope.get("total_cost_usd") is not None:
        print(f"  cost       : ${envelope['total_cost_usd']:.4f}")
    if envelope.get("permission_denials"):
        print(f"  DENIED     : {len(envelope['permission_denials'])} tool call(s) were blocked")
    for problem in problems:
        print(f"  problem    : {problem}")
    print(f"  written    : {out / 'assessment.json'}")
    return 0


def _sha256(data: bytes) -> str:
    import hashlib  # noqa: PLC0415

    return hashlib.sha256(data).hexdigest()


def _host_version(host: str) -> str:
    try:
        out = subprocess.run(  # noqa: S603
            [host, "--version"], capture_output=True, text=True, timeout=30
        )
        return (out.stdout or "").strip().split()[0] or "unknown"
    except Exception:
        return "unknown"


def _reported_model(envelope: dict[str, Any]) -> str | None:
    usage = envelope.get("modelUsage")
    if isinstance(usage, dict) and usage:
        return sorted(usage)[0]
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--ingest", type=Path, required=True, help="ingest output directory")
    parser.add_argument("--out", type=Path, required=True, help="where to write the run record")
    parser.add_argument("--mode", default="structured", help="constraint mode")
    parser.add_argument("--model", default=None, help="model id; omitted uses the host default")
    parser.add_argument("--host", default="claude", help="host CLI on PATH")
    parser.add_argument("--permission-mode", default=None, help="passed to the host")
    parser.add_argument("--timeout", type=int, default=900, help="seconds")
    return run_cell(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
