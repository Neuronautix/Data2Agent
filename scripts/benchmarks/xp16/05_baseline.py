"""Deterministic baseline: answer the XP16 questions through the Data2Agent service.

Public and data-free. No LLM is involved. For every question, the *same* gold
computation the builder runs is evaluated again, but every table row now comes
from :class:`data2agent.mcp.service.DatasetService` (``read_rows``) instead of
the independent reader. Retrieval is the only thing that changes, so a question
that passes here is one whose data the service exposes correctly today, and a
question that fails names the retrieval gap that broke it.

    python scripts/benchmarks/xp16/05_baseline.py --package <pkg> --ingest <ingest-dir> \
        --out <pkg>/results/baseline_<date>.json

Two paths are reported per question:

``retrieval``
    gold computation over rows from ``read_rows`` -- what an agent with the
    read tools and correct arithmetic could compute. The service's own column
    names are used as-is; nothing is renamed to the gold's names. A column the
    service does not expose under the name the gold reads is a header-detection
    failure and is reported as one.

``native_aggregate``
    for aggregation questions only: one call to the service's ``aggregate``
    tool (group mean and count). It has no per-animal reduction and no SEM, so
    where the gold reduces rows to animals first, the row count it returns is
    the evidence of the unit-of-analysis gap.

Abstention and pending questions have no value to compute; for them the
baseline checks whether the *evidence* an agent would need to abstain
correctly is retrievable through the service.

``--missing-capabilities`` declares what the current build lacks; each question
is marked predicted-supported when none of its ``requires`` tags is missing, so
the report shows where the prediction and the outcome disagree.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

from xp16lib import (
    REPO,
    GoldError,
    Row,
    Table,
    _apply_derived,
    _col_letter,
    evaluate,
    load_yaml,
    norm_value,
)

sys.path.insert(0, str(REPO / "src"))

# Retrieval gaps of the service as built today (see README). unit_aggregation is
# deliberately absent: on the retrieval path the arithmetic is the caller's, and
# the service-native gap is measured separately by the aggregate probe.
DEFAULT_MISSING = ",".join(
    [
        "header_detection",
        "multi_table_sheet",
        "duplicate_headers",
        "boris_tsv_preamble",
        "boris_project",
        "crosswalk",
    ]
)
_ISO_MIDNIGHT = re.compile(r"(\d{4}-\d{2}-\d{2})T00:00:00")


def _scorer():
    spec = importlib.util.spec_from_file_location(
        "xp16_score", Path(__file__).with_name("03_score.py")
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ServiceRowSource:
    """RowSource backed by the Data2Agent service's read_rows."""

    def __init__(self, service: Any, config: dict[str, Any]) -> None:
        self.svc = service
        self.config = config
        self._raw: dict[str, tuple[list[str], dict[str, int], list[dict[str, Any]]]] = {}
        self._tables: dict[str, Table] = {}

    def file_sha(self, file: str) -> str:
        entry = next((f for f in self.svc.list_files() if f["path"] == file), None)
        if entry is None:
            raise KeyError(f"service does not inventory {file!r}")
        return entry["sha256"]

    def _fetch(self, key: str) -> tuple[list[str], dict[str, int], list[dict[str, Any]]]:
        if key not in self._raw:
            profile = self.svc.manifest.get("tables", {}).get(key)
            if not isinstance(profile, dict):
                raise KeyError(f"service exposes no table {key!r}")
            positions = {
                c["name"]: int(c.get("position", i)) for i, c in enumerate(profile["columns"])
            }
            rows: list[dict[str, Any]] = []
            offset = 0
            while True:
                page = self.svc.read_rows(key, offset=offset, limit=1000)
                if not page["integrity"]["matches"]:
                    raise KeyError(f"service withheld {key!r}: integrity mismatch")
                rows.extend(page["rows"])
                offset += page["returned"]
                if not page.get("has_more") or not page["returned"]:
                    break
            self._raw[key] = (list(page["columns"]), positions, rows)
        return self._raw[key]

    def table(self, table_id: str) -> Table:
        if table_id in self._tables:
            return self._tables[table_id]
        spec = self.config["tables"][table_id]
        key = spec["service_table"]
        columns, positions, raw = self._fetch(key)
        delimited = spec.get("kind") == "delimited"
        letters = {
            name: (str(positions[name] + 1) if delimited else _col_letter(positions[name] + 1))
            for name in columns
        }
        first = int(spec.get("first_row", spec["header_rows"][-1] + 1))
        last = spec.get("last_row")
        rows = []
        for r in raw:
            loc = int(r["source_row"])
            if loc < first or (last is not None and loc > int(last)):
                continue
            values = {name: _svc_value(r["values"].get(name)) for name in columns}
            if all(v is None or v == "" for v in values.values()):
                continue
            cells = {
                name: (
                    f"line {loc}, field {letters[name]}" if delimited else f"{letters[name]}{loc}"
                )
                for name in columns
            }
            rows.append(Row(values, loc, cells))
        file = spec["file"]
        table = Table(
            table_id,
            file,
            self.file_sha(file),
            None if delimited else spec["sheet"],
            list(columns),
            letters,
            rows,
            list(spec["header_rows"]),
        )
        derived = {
            name: tpl
            for name, tpl in spec.get("derived", {}).items()
            if all(f in columns for f in re.findall(r"{([^}]+)}", tpl))
        }
        _apply_derived(table, derived)
        self._tables[table_id] = table
        return table

    def raw_cell(self, file: str, sheet: str, cell: str) -> Any:
        key = f"{file}#{sheet}"
        profile = self.svc.manifest.get("tables", {}).get(key)
        if not isinstance(profile, dict):
            raise KeyError(f"service exposes no table {key!r}")
        m = re.fullmatch(r"([A-Z]{1,3})(\d+)", cell)
        if not m:
            raise KeyError(f"bad cell {cell!r}")
        from openpyxl.utils import column_index_from_string  # noqa: PLC0415

        col_pos = column_index_from_string(m.group(1)) - 1
        row = int(m.group(2))
        columns, positions, raw = self._fetch(key)
        name = next((n for n, p in positions.items() if p == col_pos), None)
        header_row = int(profile.get("header_row") or 1)
        if row < header_row:
            raise KeyError(
                f"row {row} lies above the service's header row {header_row}; not exposed"
            )
        if row == header_row:
            # the header cell was consumed as a column name; a letter fallback means it was blank
            return None if name in (None, m.group(1)) else _svc_value(name)
        hit = next((r for r in raw if int(r["source_row"]) == row), None)
        if hit is None or name is None:
            return None
        return _svc_value(hit["values"].get(name))


def _svc_value(v: Any) -> Any:
    if isinstance(v, str):
        m = _ISO_MIDNIGHT.fullmatch(v)
        if m:
            return m.group(1)
    return norm_value(v)


def _to_service_filters(where: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    mapping = {
        "eq": "eq",
        "ne": "ne",
        "in": "in",
        "not_in": "not_in",
        "gt": "gt",
        "lt": "lt",
        "ge": "gte",
        "le": "lte",
        "nonempty": "is_not_missing",
        "empty": "is_missing",
    }
    out = []
    for rule in where or []:
        op = mapping.get(rule.get("op", "eq"))
        if op is None:
            return None  # not expressible in the service's closed registry
        item = {"column": rule["col"], "op": op}
        if "value" in rule:
            item["value"] = rule["value"]
        out.append(item)
    return out


def native_aggregate_probe(
    svc: Any, src: ServiceRowSource, config: dict[str, Any], spec: dict[str, Any] | None, gold: Any
) -> dict[str, Any] | None:
    """One call to the service's aggregate tool for a single group_stats question."""
    if not spec or spec.get("op") != "group_stats" or spec.get("join"):
        return None
    tspec = config["tables"][spec["table"]]
    _, _, raw = src._fetch(tspec["service_table"])
    first = int(tspec.get("first_row", tspec["header_rows"][-1] + 1))
    last = tspec.get("last_row")
    outside = sum(
        1
        for r in raw
        if (int(r["source_row"]) < first or (last is not None and int(r["source_row"]) > int(last)))
        and any(v not in (None, "") for v in r["values"].values())
    )
    if outside:
        return {
            "status": "mixes_blocks",
            "reason": f"the service table holds {outside} non-empty row(s) outside the declared "
            "data rows (banner/header rows or other session blocks); aggregate cannot "
            "restrict rows by position",
        }
    filters = _to_service_filters(spec.get("where"))
    if filters is None:
        return {
            "status": "not_expressible",
            "reason": "filter operator outside the service registry",
        }
    try:
        res = svc.aggregate(
            tspec["service_table"],
            group_by=list(spec.get("by", [])),
            metrics=[{"op": "count"}, {"op": "mean", "column": spec["value"]}],
            filters=filters,
        )
    except (KeyError, ValueError) as exc:
        return {"status": "error", "reason": str(exc)}
    groups = [
        {**g["group"], "n": g["metrics"]["count"], "mean": g["metrics"][f"mean:{spec['value']}"]}
        for g in res["groups"]
    ]
    gold_n = (
        {tuple(str(r.get(b)) for b in spec.get("by", [])): r.get("n") for r in gold}
        if isinstance(gold, list)
        else {}
    )
    native_n = {tuple(str(r.get(b)) for b in spec.get("by", [])): r["n"] for r in groups}
    return {
        "status": "ok",
        "groups": groups,
        "n_matches_gold": native_n == gold_n,
        "note": "service aggregate counts rows and returns no SEM; "
        + (
            "rows == units here"
            if native_n == gold_n
            else "rows != animals: the unit-of-analysis gap"
        ),
    }


def classify(reason: str, config: dict[str, Any], svc: Any) -> str:
    """Name the retrieval gap behind a failure, from the error and the declarations."""
    if "no column" in reason and ("column_" in reason or "Subjects:" in reason):
        return "boris_tsv_preamble"
    if "no table" in reason:
        return "not_inventoried"
    if "no column" in reason or "above the service's header row" in reason:
        table_id = reason.split(":", 1)[0].strip()
        spec = config.get("tables", {}).get(table_id)
        if spec is not None and spec.get("kind", "xlsx") == "xlsx":
            profile = svc.manifest.get("tables", {}).get(spec["service_table"], {})
            siblings = [
                t
                for t in config["tables"].values()
                if t.get("service_table") == spec["service_table"] and t is not spec
            ]
            if siblings and spec["header_rows"][-1] > int(profile.get("header_row") or 1) + 2:
                return "multi_table_sheet"
        return "header_detection"
    return "other"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--ingest", type=Path, required=True, help="data2agent ingest output dir")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--mode", default="structured")
    parser.add_argument("--missing-capabilities", default=DEFAULT_MISSING)
    args = parser.parse_args()

    from data2agent.mcp.service import DatasetService  # noqa: PLC0415

    pkg = args.package.resolve()
    config = load_yaml(pkg / "config" / "tables.yaml")
    spec_by_id = {
        q["id"]: q for q in load_yaml(pkg / "config" / "questions.spec.yaml")["questions"]
    }
    gold = load_yaml(pkg / "gold" / "questions.yaml")
    svc = DatasetService(args.ingest.resolve(), source_dir=pkg / "source", mode=args.mode)
    if svc.dataset_id != gold["dataset_id"]:
        sys.exit(f"ingest dataset_id {svc.dataset_id} != gold {gold['dataset_id']}")
    scorer = _scorer()
    src = ServiceRowSource(svc, config)
    missing = set(filter(None, args.missing_capabilities.split(",")))

    rows = []
    for q in gold["questions"]:
        compute = spec_by_id[q["id"]].get("compute")
        predicted = not (set(q["requires"]) & missing)
        rec: dict[str, Any] = {
            "id": q["id"],
            "category": q["category"],
            "status": q["status"],
            "requires": q["requires"],
            "predicted_supported": predicted,
        }
        try:
            result = evaluate(src, config, compute)
            answer, err = result.answer, None
        except (GoldError, KeyError, ValueError) as exc:
            answer, err = None, str(exc).strip("\"'")
        if q["status"] == "answerable":
            if err:
                rec["retrieval"] = {
                    "outcome": "fail",
                    "reason": err,
                    "gap": classify(err, config, svc),
                }
            else:
                ok, why = scorer.compare(q["expected"], answer, scorer.default_rule(q))
                rec["retrieval"] = {"outcome": "pass" if ok else "wrong", "answer": answer}
                if why:
                    rec["retrieval"]["detail"] = why
            probe = native_aggregate_probe(svc, src, config, compute, q["expected"])
            if probe is None and compute and compute.get("op") == "bundle":
                parts = {
                    name: native_aggregate_probe(
                        svc, src, config, sub, (q["expected"] or {}).get(name)
                    )
                    for name, sub in compute["parts"].items()
                }
                probe = {k: v for k, v in parts.items() if v is not None} or None
            if probe is not None:
                rec["native_aggregate"] = probe
        else:
            # abstain / pending: is the evidence (or the provisional computation) retrievable?
            if err:
                rec["evidence"] = {
                    "outcome": "not_retrievable",
                    "reason": err,
                    "gap": classify(err, config, svc),
                }
            else:
                rec["evidence"] = {"outcome": "retrievable"}
                if q["status"] == "pending":
                    same = json.dumps(answer, sort_keys=True, default=str) == json.dumps(
                        q.get("provisional_answer"), sort_keys=True, default=str
                    )
                    rec["evidence"]["matches_provisional"] = same
        rows.append(rec)

    answerable = [r for r in rows if r["status"] == "answerable"]
    summary = {
        "answerable": len(answerable),
        "retrieval_pass": sum(r["retrieval"]["outcome"] == "pass" for r in answerable),
        "retrieval_wrong": sum(r["retrieval"]["outcome"] == "wrong" for r in answerable),
        "retrieval_fail": sum(r["retrieval"]["outcome"] == "fail" for r in answerable),
        "predicted_supported": sum(r["predicted_supported"] for r in answerable),
        "predicted_supported_and_pass": sum(
            r["predicted_supported"] and r["retrieval"]["outcome"] == "pass" for r in answerable
        ),
        "unpredicted_pass": [
            r["id"]
            for r in answerable
            if not r["predicted_supported"] and r["retrieval"]["outcome"] == "pass"
        ],
        "predicted_but_failed": [
            r["id"]
            for r in answerable
            if r["predicted_supported"] and r["retrieval"]["outcome"] != "pass"
        ],
        "gaps": {},
        "unanswerable_evidence_retrievable": sum(
            r.get("evidence", {}).get("outcome") == "retrievable"
            for r in rows
            if r["status"] != "answerable"
        ),
        "unanswerable": sum(r["status"] != "answerable" for r in rows),
    }
    for r in rows:
        gap = (r.get("retrieval") or r.get("evidence") or {}).get("gap")
        if gap:
            summary["gaps"][gap] = summary["gaps"].get(gap, 0) + 1
    report = {
        "dataset_id": gold["dataset_id"],
        "service_mode": args.mode,
        "data2agent_src": str(Path(sys.modules["data2agent"].__file__).parent),
        "missing_capabilities_declared": sorted(missing),
        "summary": summary,
        "questions": rows,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    for r in rows:
        path = r.get("retrieval") or r.get("evidence")
        why = str(path.get("reason", path.get("detail", "")))[:110]
        print(
            f"  {r['id']:<8} {r['status']:<10} pred={'Y' if r['predicted_supported'] else 'n'} "
            f"{path['outcome']:<16} {path.get('gap', '')} {why}"
        )
    print("wrote", args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
