"""``console``, ``capture`` and ``sweep``: getting bytes off a device and into a file."""

from __future__ import annotations

import argparse
from pathlib import Path

from ciscoyoke.capture.commands import do_capture, do_console, do_sweep
from ciscoyoke.cli.common import Subparsers, emit_result, guard, target
from ciscoyoke.result.schema import Result
from ciscoyoke.transport.serial_ import DEFAULT_BAUD


def cmd_console(args: argparse.Namespace) -> int:
    def run() -> int:
        return do_console(
            target(args.port),
            args.baud,
            Path(args.record) if args.record else None,
        )

    return guard(run, args, "console")


def cmd_capture(args: argparse.Namespace) -> int:
    """Listen and record. The safest thing to point at unknown hardware."""

    def run() -> int:
        rendered, payload, code = do_capture(
            target(args.port),
            Path(args.output),
            baud=args.baud,
            duration=args.duration,
            quiet_after=None if args.wait else args.quiet_after,
            echo=not args.no_echo,
        )
        return emit_result(Result("capture", int(code), payload), args.json, rendered)

    return guard(run, args, "capture")


def cmd_sweep(args: argparse.Namespace) -> int:
    """Find the line speed by listening at each candidate rate."""

    def run() -> int:
        rendered, payload, code = do_sweep(
            target(args.port), seconds=args.seconds
        )
        return emit_result(Result("sweep", int(code), payload), args.json, rendered)

    return guard(run, args, "sweep")


def register(sub: Subparsers) -> None:
    console = sub.add_parser("console", help="interactive console with recording")
    console.add_argument("port")
    console.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    console.add_argument("--record", help="write a raw transcript here")
    console.set_defaults(handler=cmd_console)

    capture = sub.add_parser(
        "capture", help="listen and record without transmitting anything"
    )
    capture.add_argument("port")
    capture.add_argument("-o", "--output", required=True, help="raw .ytx to write")
    capture.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    capture.add_argument(
        "--duration", type=float, help="stop after this many seconds"
    )
    capture.add_argument(
        "--quiet-after",
        type=float,
        default=5.0,
        help="stop once the device has been silent this long (default 5s)",
    )
    capture.add_argument(
        "--wait",
        action="store_true",
        help="never stop on silence -- wait for a power-cycle, Ctrl-C to end",
    )
    capture.add_argument(
        "--no-echo", action="store_true", help="do not display output live"
    )
    capture.set_defaults(handler=cmd_capture)

    sweep = sub.add_parser(
        "sweep", help="find the line speed by listening at each candidate rate"
    )
    sweep.add_argument("port")
    sweep.add_argument(
        "--seconds", type=float, default=3.0, help="listen time per rate"
    )
    sweep.set_defaults(handler=cmd_sweep)
