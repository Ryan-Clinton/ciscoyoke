"""``lab verify`` and ``lab apply``: physical lab topology."""

from __future__ import annotations

import argparse
from pathlib import Path

from ciscoyoke import labs
from ciscoyoke.cli.common import Subparsers, emit_result, guard
from ciscoyoke.result.schema import Result
from ciscoyoke.transport.serial_ import DEFAULT_BAUD


def cmd_lab_verify(args: argparse.Namespace) -> int:
    def run() -> int:
        rendered, payload, code = labs.do_lab_verify(
            Path(args.labfile), baud=args.baud
        )
        return emit_result(Result("lab verify", int(code), payload), args.json, rendered)

    return guard(run, args, "lab verify")


def cmd_lab_apply(args: argparse.Namespace) -> int:
    def run() -> int:
        rendered, payload, code = labs.do_lab_apply(
            Path(args.labfile),
            baud=args.baud,
            confirm=args.confirm,
            allow_weak=args.allow_weak_match,
        )
        return emit_result(Result("lab apply", int(code), payload), args.json, rendered)

    return guard(run, args, "lab apply")


def register(sub: Subparsers) -> None:
    lab_parser = sub.add_parser("lab", help="physical lab topology")
    lab_sub = lab_parser.add_subparsers(dest="subcommand", required=True)

    verify = lab_sub.add_parser("verify", help="check cabling against a lab file")
    verify.add_argument("labfile")
    verify.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    verify.set_defaults(handler=cmd_lab_verify)

    apply_parser = lab_sub.add_parser("apply", help="push configuration to the lab")
    apply_parser.add_argument("labfile")
    apply_parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    apply_parser.add_argument("--confirm", action="store_true")
    apply_parser.add_argument(
        "--allow-weak-match",
        action="store_true",
        help="write to devices matched only by port name",
    )
    apply_parser.set_defaults(handler=cmd_lab_apply)
