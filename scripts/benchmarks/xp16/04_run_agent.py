"""Run one agent condition on the XP16 questions through the Data2Agent MCP server.

Public and data-free. A thin stub over the XP14 harness: the MCP config, the
per-mode tool allowlist, JSON recovery from the reply and the host/model
reporting are imported from ``scripts/benchmarks/xp14/08_run_agent.py`` rather
than copied, so both benchmarks isolate the MCP surface the same way
(``--mcp-config`` + ``--strict-mcp-config``, never a global registration).

    python scripts/benchmarks/xp16/04_run_agent.py --package <pkg> --ingest <ingest-dir> \
        --out <run-dir> --mode structured --model <model-id> [--capabilities a,b,...]
    python scripts/benchmarks/xp16/03_score.py --questions <pkg>/gold/questions.yaml \
        --answers <run-dir>/answers.json --package <pkg>

The prompt is ``prompt_qa.md`` with the questions substituted in. Only a
question's id, text and answer_type are sent -- never its expected answer,
sources, computation, status or capability tags: those are the measurement.
``--capabilities`` restricts the run to questions whose every ``requires`` tag
is in the set (the subset a build is expected to handle). ``--dry-run`` writes
the prompt and exits, so the instrument can be reviewed without a host.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
PROMPT = HERE / "prompt_qa.md"


def _xp14_harness():
    path = HERE.parent / "xp14" / "08_run_agent.py"
    spec = importlib.util.spec_from_file_location("xp14_run_agent", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def build_prompt(
    questions: list[dict[str, Any]], capabilities: set[str] | None
) -> tuple[str, list[str]]:
    chosen = [
        q for q in questions if capabilities is None or set(q.get("requires", [])) <= capabilities
    ]
    public = [
        {"id": q["id"], "question": q["question"], "answer_type": q["answer_type"]} for q in chosen
    ]
    text = PROMPT.read_text(encoding="utf-8").replace(
        "{questions_json}", json.dumps(public, indent=1, ensure_ascii=False)
    )
    return text, [q["id"] for q in chosen]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--ingest", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--mode", default="structured")
    parser.add_argument("--model", default=None)
    parser.add_argument("--host", default="claude")
    parser.add_argument("--permission-mode", default=None)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--capabilities", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    import yaml  # noqa: PLC0415

    gold = yaml.safe_load((args.package / "gold" / "questions.yaml").read_text(encoding="utf-8"))
    caps = set(args.capabilities.split(",")) if args.capabilities else None
    prompt, ids = build_prompt(gold["questions"], caps)
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    (out / "prompt.md").write_text(prompt, encoding="utf-8")
    print(f"  questions  : {len(ids)}")
    if args.dry_run:
        print(f"  prompt     : {out / 'prompt.md'} (dry run, no host called)")
        return 0

    xp14 = _xp14_harness()
    ingest = args.ingest.resolve()
    manifest = json.loads((ingest / "manifest.json").read_text(encoding="utf-8"))
    if manifest.get("dataset_id") != gold["dataset_id"]:
        sys.exit("ingest and gold disagree on dataset_id; refusing to run")
    host = shutil.which(args.host)
    if host is None:
        sys.exit(f"host not on PATH: {args.host}")
    config = xp14.write_mcp_config(out / "mcp-config.json", ingest, args.mode)
    tools = xp14.mode_tools(args.mode)
    command = [
        host, "-p", prompt,
        "--mcp-config", str(config), "--strict-mcp-config",
        "--allowed-tools", ",".join(f"{xp14._TOOL_PREFIX}{t}" for t in tools),
        "--output-format", "json",
    ]  # fmt: skip
    if args.model:
        command += ["--model", args.model]
    if args.permission_mode:
        command += ["--permission-mode", args.permission_mode]

    started = time.time()
    done = subprocess.run(  # noqa: S603
        command, capture_output=True, text=True, timeout=args.timeout,
        stdin=subprocess.DEVNULL, cwd=str(out),
    )  # fmt: skip
    elapsed = round(time.time() - started, 1)
    (out / "host-response.json").write_text(done.stdout or "", encoding="utf-8")
    if done.returncode != 0:
        print(f"  host exited {done.returncode}\n{(done.stderr or '')[:2000]}")
        return 1
    envelope = json.loads(done.stdout)
    reply = envelope.get("result", "")
    payload = xp14.extract_json(reply if isinstance(reply, str) else json.dumps(reply))
    if payload is None or not isinstance(payload.get("answers"), dict):
        (out / "agent-reply.txt").write_text(str(reply), encoding="utf-8")
        print("  the agent's reply had no 'answers' object; kept at agent-reply.txt")
        return 1
    unknown = sorted(set(payload["answers"]) - set(ids))
    (out / "answers.json").write_text(
        json.dumps({"answers": payload["answers"]}, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    (out / "run.json").write_text(
        json.dumps(
            {
                "dataset_id": gold["dataset_id"],
                "mode": args.mode,
                "host": f"{args.host}/{xp14._host_version(host)}",
                "model": args.model or xp14._reported_model(envelope) or "host-default",
                "prompt_sha256": xp14._sha256(prompt.encode("utf-8")),
                "questions": ids,
                "capabilities_filter": sorted(caps) if caps else None,
                "tools_allowed": tools,
                "elapsed_seconds": elapsed,
                "cost_usd": envelope.get("total_cost_usd"),
                "usage": envelope.get("usage"),
                "permission_denials": envelope.get("permission_denials"),
                "answers_for_unknown_ids": unknown,
                "ran_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"  answered   : {len(payload['answers'])}/{len(ids)} in {elapsed}s")
    print(f"  written    : {out / 'answers.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
