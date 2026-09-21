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
    ingest_parser.set_defaults(handler=_cmd_ingest)

    verify_parser = subparsers.add_parser(
        "verify", help="re-checksum the source against the manifest"
    )
    verify_parser.add_argument("output", type=Path, help="an ingest output directory")
    verify_parser.add_argument(
        "--source", type=Path, default=None, help="override the recorded source"
    )
    verify_parser.set_defaults(handler=_cmd_verify)

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
    result = ingest(args.source, args.output)
    if not args.no_report:
        write_report(result.output_dir, result.manifest, result.evidence)
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
