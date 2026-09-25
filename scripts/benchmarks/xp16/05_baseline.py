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
    read tools and correct arithmetic could compute. A gold column is found in
    the service table only at the SAME physical column, and only if the gold's
    header parts are a suffix of the service's (the service may carry extra
    upper labels, never fewer; dedupe suffixes '.N' / ' #N' are ignored). So
    naming conventions do not fail a question, but a wrong header row, a missing
    upper label, or a same-named column elsewhere in the sheet still does -- the
    last one is what name-only matching got wrong (``--names-as-is`` keeps that
    behaviour for comparison). Each run records its column map.

The ingest condition (declared layout / relationships / crosswalk sha256s, as
written by ``06_ingest.py`` to ``condition.json``) is copied into the report,
together with the status of every declared relationship.

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
    FileRowSource,
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

# Retrieval gaps of the service as built on main after D2A-97/98/99/100 (header
# detection and --layout, crosswalks, unit aggregation, .boris projects): only
# declared multi-block sheets remain. Pass --missing-capabilities to model an
# older or newer build. unit_aggregation is deliberately never listed: on the
# retrieval path the arithmetic is the caller's, and the service-native gap is
# measured separately by the aggregate probe.
DEFAULT_MISSING = "multi_table_sheet"
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

    def __init__(self, service: Any, config: dict[str, Any], gold: Any = None) -> None:
        self.svc = service
        self.config = config
        # The gold reader's declared tables, used ONLY for where each gold column
        # physically sits and what its header cells say -- never for values.
        self.gold = gold
        self._raw: dict[str, tuple[list[str], dict[str, int], list[dict[str, Any]]]] = {}
        self._tables: dict[str, Table] = {}
        self.column_maps: dict[str, dict[str, Any]] = {}
        self.renames: dict[str, dict[str, str]] = {}  # table id -> {service name: gold name}
        self.tools_used: set[str] = {"read_rows"}
        self.uncovered_cells: list[str] = []
        self.transform_crosswalks: dict[str, str] = {}  # gold transform -> service crosswalk

    def service_name(self, table_id: str, gold_name: str) -> str:
        """The service's name for a gold column (after table() has resolved it)."""
        self.table(table_id)
        for service, gold in self.renames.get(table_id, {}).items():
            if gold == gold_name:
                return service
        return gold_name

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

    def _resolve_columns(
        self, table_id: str, columns: list[str], positions: dict[str, int], delimited: bool
    ) -> dict[str, str]:
        """Service column name -> gold column name, by physical column and header text.

        A gold column is found in the service table only when the service has a
        column at the same physical position whose header parts END WITH the gold
        column's header parts (the service may carry extra upper labels, never
        fewer; dedupe suffixes on either side are ignored). So a naming
        convention (' / ' vs ' | ', '.1' vs ' #2') does not fail a question, but
        a wrong header row, or a missing upper label such as a BORIS behaviour
        name, still does. Without a gold reader, names are used as they come.
        """
        if self.gold is None:
            return {}
        gold_table = self.gold.table(table_id)
        by_position = {p: n for n, p in positions.items()}
        rename: dict[str, str] = {}
        unmatched = []
        for gname in gold_table.columns:
            letter = gold_table.column_letters.get(gname, "")
            if "+" in letter:
                continue  # derived from other columns; rebuilt from them below
            pos = int(letter) - 1 if delimited else _col_index(letter) - 1
            sname = by_position.get(pos)
            if sname is not None and _labels_compatible(gname, sname, set(columns)):
                rename[sname] = gname
            else:
                unmatched.append({"gold": gname, "position": pos + 1, "service": sname})
        # an unmatched service column must never stand in for a gold column by name
        gold_names = set(gold_table.columns)
        for name in columns:
            if name not in rename and name in gold_names:
                rename[name] = f"{name} [service label at another position]"
        self.column_maps[table_id] = {
            "service_table": self.config["tables"][table_id]["service_table"],
            "matched": sum(1 for v in rename.values() if not v.endswith("position]")),
            "unmatched": unmatched,
        }
        return rename

    def table(self, table_id: str) -> Table:
        if table_id in self._tables:
            return self._tables[table_id]
        spec = self.config["tables"][table_id]
        key = spec["service_table"]
        columns, positions, raw = self._fetch(key)
        delimited = spec.get("kind") == "delimited"
        rename = self._resolve_columns(table_id, columns, positions, delimited)
        self.renames[table_id] = rename
        letters = {
            rename.get(name, name): (
                str(positions[name] + 1) if delimited else _col_letter(positions[name] + 1)
            )
            for name in columns
        }
        first = int(spec.get("first_row", spec["header_rows"][-1] + 1))
        last = spec.get("last_row")
        rows = []
        for r in raw:
            loc = int(r["source_row"])
            if loc < first or (last is not None and loc > int(last)):
                continue
            values = {rename.get(name, name): _svc_value(r["values"].get(name)) for name in columns}
            if all(v is None or v == "" for v in values.values()):
                continue
            cells = {
                name: (
                    f"line {loc}, field {letters[name]}" if delimited else f"{letters[name]}{loc}"
                )
                for name in values
            }
            rows.append(Row(values, loc, cells))
        file = spec["file"]
        table = Table(
            table_id,
            file,
            self.file_sha(file),
            None if delimited else spec["sheet"],
            [rename.get(name, name) for name in columns],
            letters,
            rows,
            list(spec["header_rows"]),
        )
        derived = {
            name: tpl
            for name, tpl in spec.get("derived", {}).items()
            if all(f in table.columns for f in re.findall(r"{([^}]+)}", tpl))
        }
        _apply_derived(table, derived)
        self._tables[table_id] = table
        return table

    def raw_cell(self, file: str, sheet: str, cell: str) -> Any:
        """One cell of a sheet, read through the service as an agent would.

        Data rows come from read_rows; a row above a table's data (a banner, a
        block title, a raw header row) from inspect_table(include_rows_above_data).
        When the sheet is declared as blocks it is no longer one table: the cell
        is looked up in the block whose rows cover it. A row no block covers (a
        blank spacer between blocks) reads as empty and is listed in the report
        under ``uncovered_cells``, so that reading is visible, not silent.
        """
        m = re.fullmatch(r"([A-Z]{1,3})(\d+)", cell)
        if not m:
            raise KeyError(f"bad cell {cell!r}")
        letter, row = m.group(1), int(m.group(2))
        key = f"{file}#{sheet}"
        tables = self.svc.manifest.get("tables", {})
        if isinstance(tables.get(key), dict):
            return self._cell(key, letter, row, strict=True)[1]
        blocks = sorted(k for k in tables if k.startswith(f"{key}#"))
        if not blocks:
            raise KeyError(f"service exposes no table {key!r}")
        for block in blocks:
            covered, value = self._cell(block, letter, row, strict=False)
            if covered:
                self.tools_used.add("declared blocks (<file>#<sheet>#<block>)")
                return value
        self.uncovered_cells.append(f"{key}!{cell}")
        return None

    def _cell(self, key: str, letter: str, row: int, *, strict: bool) -> tuple[bool, Any]:
        """(does this table cover the row?, value). Strict: the table is the whole sheet."""
        profile = self.svc.manifest["tables"][key]
        col_pos = _col_index(letter) - 1
        columns, positions, raw = self._fetch(key)
        name = next((n for n, p in positions.items() if p == col_pos), None)
        header_row = int(profile.get("header_row") or 1)
        data_rows = [int(r["source_row"]) for r in raw]
        first_data = min(data_rows, default=header_row + 1)
        last_data = max(data_rows, default=header_row)
        if row < first_data:
            # Not an observation: a banner / preamble row the header rule skipped, or a
            # raw header row. An agent reads those through inspect_table (D2A-102).
            above = self.svc.inspect_table(key, include_rows_above_data=True)
            self.tools_used.add("inspect_table(include_rows_above_data=True)")
            if not (above.get("integrity") or {}).get("matches", True):
                raise KeyError(f"service withheld rows above the data of {key!r}")
            for entry in above.get("rows_above_data", []):
                if int(entry["row"]) == row:
                    for c in entry.get("cells", []):
                        if c.get("column_letter") == letter:
                            return True, _svc_value(c.get("value"))
                    if entry.get("cells_truncated"):
                        raise KeyError(f"row {row} of {key!r}: cells truncated by the service")
                    return True, None
            if row == header_row:
                # a single header row is consumed as column names; a letter name means blank
                return True, (None if name in (None, letter) else _svc_value(name))
            if not strict:
                return False, None
            raise KeyError(
                f"row {row} of {key!r} lies above the data and inspect_table does not expose it"
            )
        if not strict and row > last_data:
            return False, None
        hit = next((r for r in raw if int(r["source_row"]) == row), None)
        if hit is None or name is None:
            return True, None
        return True, _svc_value(hit["values"].get(name))


_GOLD_DEDUPE = re.compile(r" #\d+$")
_SERVICE_DEDUPE = re.compile(r"^(.*)\.\d+$")


def _labels_compatible(gold_name: str, service_name: str, service_names: set[str]) -> bool:
    """Do the gold header parts form a suffix of the service header parts?

    Dedupe suffixes are removed first: ' #N' on the gold side; '.N' on the
    service side only when the name without it is itself a column of that table
    (that is how the service de-duplicates), so a numeric header such as '0.4'
    is never mistaken for a suffixed '0'.
    """
    base = _SERVICE_DEDUPE.match(service_name)
    if base and base.group(1) in service_names:
        service_name = base.group(1)
    gold = [" ".join(p.split()) for p in _GOLD_DEDUPE.sub("", gold_name).split(" | ")]
    service = [" ".join(p.split()) for p in service_name.split(" / ")]
    gold = [p for p in gold if p]
    service = [p for p in service if p]
    return len(gold) <= len(service) and service[len(service) - len(gold) :] == gold


def _col_index(letter: str) -> int:
    from openpyxl.utils import column_index_from_string  # noqa: PLC0415

    return column_index_from_string(letter)


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
    svc: Any,
    src: ServiceRowSource,
    config: dict[str, Any],
    spec: dict[str, Any] | None,
    gold: Any,
    tolerance: float = 0.01,
) -> dict[str, Any] | None:
    """One service call for one group_stats computation, scored against the gold.

    The service's own tools do the whole computation: ``aggregate`` with a
    declared ``unit`` (per-animal reduction, then mean / sem across units), or
    ``aggregate_join`` when the grouping column lives in another table -- through
    the declared crosswalk when the gold join uses an identifier transform (the
    ingest condition maps the transform to a crosswalk: ``transform_crosswalks``
    in condition.json, so the gold config is untouched). Column names are
    the service's own, resolved structurally as for the retrieval path.
    """
    if not spec or spec.get("op") != "group_stats":
        return None
    tid = spec["table"]
    tspec = config["tables"][tid]
    try:
        src.table(tid)
    except KeyError as exc:
        return {"status": "error", "reason": str(exc)}
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
            "data rows (other session blocks); aggregate cannot restrict rows by position",
        }

    def left(col: str) -> str:
        return src.service_name(tid, col)

    by = list(spec.get("by", []))
    units = [spec["unit"]] if isinstance(spec.get("unit"), str) else list(spec.get("unit") or [])
    reduce = spec.get("reduce", "single")
    join = spec.get("join")
    where = [{**w, "col": left(w["col"])} for w in spec.get("where") or []]
    try:
        if join:
            jt = config["tables"][join["table"]]
            src.table(join["table"])

            def right(col: str) -> str:
                return src.service_name(join["table"], col)

            named = join.get("right_transform") or join.get("left_transform")
            crosswalk = src.transform_crosswalks.get(named) if named else None
            if named and not crosswalk:
                return {
                    "status": "not_expressible",
                    "reason": f"the ingest condition declares no service crosswalk for {named!r}",
                }
            # the gold join may restrict the right table too (e.g. one genotype)
            right_where = [
                {**w, "col": f"right.{right(w['col'])}"} for w in join.get("where") or []
            ]
            filters = _to_service_filters(
                [{**w, "col": f"left.{w['col']}"} for w in where] + right_where
            )
            if filters is None:
                return {"status": "not_expressible", "reason": "filter outside the registry"}
            value = f"left.{left(spec['value'])}"
            group_by = [
                f"right.{right(c)}" if c in join["columns"] else f"left.{left(c)}" for c in by
            ]
            unit_cols = [f"left.{left(u)}" for u in units]
            res = svc.aggregate_join(
                left=tspec["service_table"],
                right=jt["service_table"],
                left_keys=[left(join["left_key"])],
                right_keys=[right(join["right_key"])],
                crosswalk=crosswalk,
                group_by=group_by,
                filters=filters,
                **_unit_args(unit_cols, reduce, value),
            )
            src.tools_used.add("aggregate_join" + (f"(crosswalk={crosswalk})" if crosswalk else ""))
        else:
            filters = _to_service_filters(where)
            if filters is None:
                return {"status": "not_expressible", "reason": "filter outside the registry"}
            res = svc.aggregate(
                tspec["service_table"],
                group_by=[left(b) for b in by],
                filters=filters,
                **_unit_args([left(u) for u in units], reduce, left(spec["value"])),
            )
            src.tools_used.add("aggregate" + ("(unit)" if units else ""))
    except (KeyError, ValueError) as exc:
        return {"status": "error", "reason": str(exc)}

    native = []
    for g in res.get("groups", []):
        mets = g.get("metrics", {})
        native.append(
            {
                "group": [str(v) for v in g.get("group", {}).values()],
                "n": g.get("n_units")
                if "n_units" in g
                else (mets.get("count") or 0)
                - next((v for k, v in mets.items() if k.startswith("n_missing:")), 0),
                "mean": next((v for k, v in mets.items() if k.startswith("mean:")), None),
                "sem": next((v for k, v in mets.items() if k.startswith("sem:")), None),
            }
        )
    expected = {tuple(str(r.get(b)) for b in by): r for r in gold} if isinstance(gold, list) else {}
    problems = []
    got = {tuple(r["group"]): r for r in native}
    for key, row in expected.items():
        hit = got.get(key)
        if hit is None:
            problems.append(f"group {key} missing")
            continue
        if hit["n"] != row.get("n"):
            problems.append(f"group {key}: n {hit['n']} != {row.get('n')}")
        for f in ("mean", "sem"):
            e, v = row.get(f), hit[f]
            if e is None and v is None:
                continue
            if e is None or v is None or abs(float(e) - float(v)) > tolerance:
                problems.append(f"group {key}: {f} {v} != {e}")
    extra = sorted(set(got) - set(expected))
    if extra:
        problems.append(f"unexpected group(s) {extra}")
    return {
        "status": "pass" if not problems else "fail",
        "groups": native,
        **({"detail": "; ".join(problems)} if problems else {}),
    }


def _probe(
    svc: Any, src: ServiceRowSource, config: dict[str, Any], compute: Any, gold: Any
) -> dict[str, Any] | None:
    """Native probe for a group_stats computation, or for each group_stats part of a bundle."""
    probe = native_aggregate_probe(svc, src, config, compute, gold)
    if probe is None and compute and compute.get("op") == "bundle":
        parts = {
            name: native_aggregate_probe(
                svc, src, config, sub, (gold or {}).get(name) if isinstance(gold, dict) else None
            )
            for name, sub in compute["parts"].items()
        }
        parts = {k: v for k, v in parts.items() if v is not None}
        if parts:
            statuses = {v["status"] for v in parts.values()}
            return {"status": "pass" if statuses == {"pass"} else "fail", "parts": parts}
    return probe


def _unit_args(unit: list[str], reduce: str, value: str) -> dict[str, Any]:
    """Metric arguments for one value: per-unit reduction first when a unit is declared."""
    if unit:
        op = reduce if reduce in {"sum", "mean"} else "mean"  # 'single': mean of one value
        return {
            "unit": unit,
            "unit_metrics": [{"op": op, "column": value, "name": "v"}],
            "metrics": [{"op": "mean", "column": "v"}, {"op": "sem", "column": "v"}],
        }
    return {
        "metrics": [
            {"op": "count"},
            {"op": "n_missing", "column": value},
            {"op": "mean", "column": value},
            {"op": "sem", "column": value},
        ]
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


def _condition(ingest: Path) -> dict[str, Any] | None:
    """The declaration record 06_ingest.py wrote, so a result names its exact condition."""
    path = ingest / "condition.json"
    if not path.exists():
        return {"condition": "unrecorded", "note": "ingest not produced by 06_ingest.py"}
    return json.loads(path.read_text(encoding="utf-8"))


def _relationships(svc: Any) -> dict[str, Any]:
    listing = svc.list_relationships()
    declared = [
        {
            "status": r["status"],
            "left": r["left"]["table"],
            "right": r["right"]["table"],
            "left_matched_distinct_keys": r["left"].get("matched_distinct_keys"),
            "right_matched_distinct_keys": r["right"].get("matched_distinct_keys"),
            "cardinality": r.get("cardinality"),
            "warnings": r.get("warnings", []),
        }
        for r in listing.get("relationships", [])
        if (r.get("basis") or {}).get("method") == "explicit-declaration"
    ]
    return {"status_counts": listing.get("status_counts"), "declared": declared}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--ingest", type=Path, required=True, help="data2agent ingest output dir")
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--mode", default="structured")
    parser.add_argument("--missing-capabilities", default=DEFAULT_MISSING)
    parser.add_argument(
        "--data2agent-src", type=Path, default=None, help="evaluate another data2agent src/ tree"
    )
    parser.add_argument(
        "--names-as-is",
        action="store_true",
        help="match gold columns to service columns by name only (the pre-D2A-105 behaviour)",
    )
    args = parser.parse_args()

    if args.data2agent_src is not None:
        # evaluate another data2agent tree (e.g. an unmerged branch); recorded in the report
        sys.path.insert(0, str(args.data2agent_src.resolve()))
        for mod in [m for m in sys.modules if m == "data2agent" or m.startswith("data2agent.")]:
            del sys.modules[mod]
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
    gold_reader = None if args.names_as_is else FileRowSource(pkg / "source", config)
    condition = _condition(args.ingest.resolve()) or {}
    # the condition may address a declared table by another service key (a block)
    for table_id, key in (condition.get("service_tables") or {}).items():
        if table_id not in config["tables"]:
            sys.exit(f"condition names a service table for unknown gold table {table_id!r}")
        config["tables"][table_id]["service_table"] = key
    src = ServiceRowSource(svc, config, gold_reader)
    src.transform_crosswalks = dict(
        (_condition(args.ingest.resolve()) or {}).get("transform_crosswalks") or {}
    )
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
            probe = _probe(svc, src, config, compute, q["expected"])
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
            if q["status"] == "pending":
                probe = _probe(svc, src, config, compute, q.get("provisional_answer"))
                if probe is not None:
                    rec["native_aggregate"] = {"against": "provisional_answer", **probe}
        rows.append(rec)

    answerable = [r for r in rows if r["status"] == "answerable"]
    probed = [r for r in rows if "native_aggregate" in r]
    summary = {
        "native_aggregate": {
            "probed": len(probed),
            "pass": sorted(r["id"] for r in probed if r["native_aggregate"]["status"] == "pass"),
            "not_pass": {
                r["id"]: r["native_aggregate"]["status"]
                for r in probed
                if r["native_aggregate"]["status"] != "pass"
            },
        },
        "service_tools_used": sorted(src.tools_used),
        "uncovered_cells": len(src.uncovered_cells),
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
        "condition": _condition(args.ingest.resolve()),
        "column_resolution": (
            "names as the service reports them"
            if args.names_as_is
            else "same physical column + gold header parts a suffix of the service's"
        ),
        "summary": summary,
        "relationships": _relationships(svc),
        "column_maps": src.column_maps,
        "uncovered_cells": src.uncovered_cells,
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
