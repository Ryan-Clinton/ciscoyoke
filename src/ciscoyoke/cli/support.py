"""``support-bundle`` and ``report``: packaging a run so it can be fixed remotely."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ciscoyoke import report, support
from ciscoyoke.cli.common import Subparsers, emit_result, guard, target
from ciscoyoke.result.exits import ExitCode
from ciscoyoke.result.schema import Result


def cmd_support_bundle(args: argparse.Namespace) -> int:
    def run() -> int:
        summary, directory = support.do_support_bundle(
            Path(args.output), target(args.port) if args.port else None
        )
        return emit_result(
            Result(
                "support-bundle",
                int(ExitCode.SUCCESS),
                {"directory": str(directory)},
            ),
            args.json,
            summary,
        )

    return guard(run, args, "support-bundle")


def cmd_report(args: argparse.Namespace) -> int:
    """Package a run for a remote fix: scrubbed, configuration output removed."""

    def run() -> int:
        try:
            outcome = report.do_report(
                Path(args.run) if args.run else None,
                Path(args.output) if args.output else None,
            )
        except FileNotFoundError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return int(ExitCode.DEVICE_NOT_FOUND)
        return emit_result(
            Result(
                "report",
                int(ExitCode.SUCCESS),
                {
                    "report": str(outcome.path),
                    "run": str(outcome.run),
                    "config_blocks_removed": outcome.elided,
                    "had_failure": outcome.had_failure,
                },
            ),
            args.json,
            outcome.render(),
        )

    return guard(run, args, "report")


def register(sub: Subparsers) -> None:
    bundle = sub.add_parser(
        "support-bundle", help="collect diagnostics for a bug report"
    )
    bundle.add_argument("-o", "--output", default="ciscoyoke-support")
    bundle.add_argument("--port", help="include any unfinished journal for this port")
    bundle.set_defaults(handler=cmd_support_bundle)

    report_parser = sub.add_parser(
        "report",
        help="package a run into one shareable zip so it can be fixed remotely",
    )
    report_parser.add_argument(
        "--run", help="raw transcript to report (default: the most recent run)"
    )
    report_parser.add_argument(
        "-o", "--output", help="directory for the zip (default: current directory)"
    )
    report_parser.set_defaults(handler=cmd_report)
