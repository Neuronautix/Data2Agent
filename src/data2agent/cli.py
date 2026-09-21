"""Command-line entry point.

data2agent ingest  <dataset> -o <output>   deterministic scan -> manifest/evidence
data2agent verify  <output>                re-checksum the source against the manifest
data2agent serve   <output> --mode <mode>  run the Data2MCP server over stdio
data2agent modes                           list benchmark modes and their status
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .errors import Data2AgentError
from .ingest import ingest
from .ingest.conventions import STRICT_CONVENTION, custom
from .ingest.pipeline import write_json
from .mcp.modes import DEFAULT_MODE, MODES
from .mcp.service import DatasetService
from .report import write_mcp_config, write_report


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except Data2AgentError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    except (KeyError, FileNotFoundError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="data2agent", description=__doc__)
    parser.add_argument("--version", action="version", version=f"data2agent {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest_parser = subparsers.add_parser("ingest", help="deterministically ingest a dataset")
    ingest_parser.add_argument("source", type=Path, help="dataset directory (read-only)")
    ingest_parser.add_argument("-o", "--output", type=Path, required=True, help="output directory")
    ingest_parser.add_argument(
        "--mode",
        default=DEFAULT_MODE,
        choices=sorted(MODES),
        help="mode recorded in the MCP config",
    )
    ingest_parser.add_argument("--no-report", action="store_true", help="skip report/ and mcp/")
    missing = ingest_parser.add_mutually_exclusive_group()
    missing.add_argument(
        "--missing-tokens",
        default=None,
        metavar="NA,null,...",
        help=(
            "comma-separated tokens to resolve to missing, overriding both the dataset's "
            "own declaration and the built-in default"
        ),
    )
    missing.add_argument(
        "--strict-missing",
        action="store_true",
        help="treat only empty cells as missing; resolve no tokens at all",
    )
    ingest_parser.set_defaults(handler=_cmd_ingest)

    verify_parser = subparsers.add_parser(
        "verify", help="re-checksum the source against the manifest"
    )
    verify_parser.add_argument("output", type=Path, help="an ingest output directory")
    verify_parser.add_argument(
        "--source", type=Path, default=None, help="override the recorded source"
    )
    verify_parser.set_defaults(handler=_cmd_verify)

    assess_parser = subparsers.add_parser("assess", help="run a profile's deterministic checks")
    assess_parser.add_argument("output", type=Path, help="an ingest output directory")
    assess_parser.add_argument("--profile", default="fair", help="profile id (default: fair)")
    assess_parser.add_argument("--rule", default=None, help="run a single rule by id")
    assess_parser.add_argument(
        "--list", action="store_true", help="list the profile's rules and exit"
    )
    assess_parser.add_argument(
        "--source", type=Path, default=None, help="override the recorded source"
    )
    assess_parser.set_defaults(handler=_cmd_assess)

    serve_parser = subparsers.add_parser("serve", help="run the Data2MCP server")
    serve_parser.add_argument("output", type=Path, help="an ingest output directory")
    serve_parser.add_argument(
        "--source", type=Path, default=None, help="override the recorded source"
    )
    serve_parser.add_argument("--mode", default=DEFAULT_MODE, choices=sorted(MODES))
    serve_parser.add_argument("--transport", default="stdio")
    serve_parser.set_defaults(handler=_cmd_serve)

    modes_parser = subparsers.add_parser("modes", help="list benchmark modes")
    modes_parser.set_defaults(handler=_cmd_modes)
    return parser


def _cmd_ingest(args: argparse.Namespace) -> int:
    convention = None
    if args.strict_missing:
        convention = STRICT_CONVENTION
    elif args.missing_tokens:
        convention = custom(args.missing_tokens.split(","))

    result = ingest(args.source, args.output, convention=convention)
    if not args.no_report:
        write_report(result.output_dir, result.manifest, result.evidence, result.provenance)
        write_mcp_config(
            result.output_dir,
            source_dir=Path(result.provenance["source_path"]),
            mode=args.mode,
            python_executable=sys.executable,
        )

    print(f"dataset_id : {result.dataset_id}")
    print(f"files      : {result.manifest['file_count']}")
    print(f"tables     : {len(result.manifest['tables'])}")
    print(f"claims     : {len(result.evidence)}")
    applied = result.manifest["missing_value_convention"]
    print(f"missing    : resolved under '{applied['id']}' ({applied['source']})")
    print(f"output     : {result.output_dir}")
    if not result.provenance.get("source_verified_unchanged", True):
        print("WARNING: the source changed during ingest; this manifest is not trustworthy")
    for warning in result.warnings:
        print(f"warning    : {warning}")
    return 0


def _cmd_verify(args: argparse.Namespace) -> int:
    service = DatasetService(args.output, source_dir=args.source)
    report = service.verify_dataset()
    print(json.dumps(report, indent=2))
    return 0 if report["intact"] else 1


def _cmd_assess(args: argparse.Namespace) -> int:
    # fair-deterministic is the reference condition: the verdicts below come
    # from code, so the mode label on the assessment must say so.
    service = DatasetService(args.output, source_dir=args.source, mode="fair-deterministic")

    if args.list:
        listing = service.list_fair_rules()
        print(f"{listing['profile']['id']} {listing['profile']['version']}")
        for rule in listing["rules"]:
            status = "" if rule["implemented"] else "  [NOT IMPLEMENTED -> always unknown]"
            print(f"  {rule['id']:<32} {rule['principle']:<6} {rule['check']}{status}")
            print(f"  {'':<32} {rule['question'].strip()}")
        return 0

    assessment = service.run_fair_check(args.rule, orchestrator="deterministic")
    destination = Path(args.output) / "assessment.json"
    write_json(destination, assessment)

    summary = assessment["summary"]
    print(f"dataset_id : {assessment['dataset_id']}")
    print(f"profile    : {assessment['profile']['id']} {assessment['profile']['version']}")
    print(
        "results    : " + ", ".join(f"{count} {name}" for name, count in summary.items() if count)
    )
    print(f"written    : {destination}")
    print()
    for result in assessment["results"]:
        print(f"  {result['rule_id']:<32} {result['result']}")
        if result.get("rationale"):
            print(f"  {'':<32} {' '.join(result['rationale'].split())}")
    # A failed indicator is a finding about the dataset, not a tool error.
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    from .mcp.server import serve

    serve(args.output, source_dir=args.source, mode=args.mode, transport=args.transport)
    return 0


def _cmd_modes(_: argparse.Namespace) -> int:
    for name in sorted(MODES):
        mode = MODES[name]
        status = (
            f"available since {mode.available_since}" if mode.available_since else "NOT IMPLEMENTED"
        )
        print(f"{name:<20} {status}")
        print(f"{'':<20} {mode.description}")
        print(f"{'':<20} tools: {', '.join(mode.tools)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
