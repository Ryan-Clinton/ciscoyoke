"""``rescue``, ``health``, ``archive``, ``reset``, ``recover access`` and ``resolve``.

The first three only look. ``reset`` and ``recover access`` change the device,
so they dry-run unless ``--confirm`` is given. ``resolve`` reconciles a run that
was interrupted partway through either.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from ciscoyoke import commands, interactive, support
from ciscoyoke.cli import image
from ciscoyoke.cli.common import Subparsers, emit_result, guard, target
from ciscoyoke.result.exits import ExitCode
from ciscoyoke.result.schema import Result
from ciscoyoke.transport.serial_ import DEFAULT_BAUD


def cmd_rescue(args: argparse.Namespace) -> int:
    def run() -> int:
        report = commands.do_rescue(args.port, args.baud)
        return emit_result(
            Result("rescue", int(ExitCode.SUCCESS), report.to_json()),
            args.json,
            report.render(),
        )

    return guard(run, args, "rescue")


def cmd_health(args: argparse.Namespace) -> int:
    def run() -> int:
        payload, code = commands.do_health(args.port, args.baud)
        lines = [f"Overall health: {payload.get('overall')}"]
        checks = payload.get("checks")
        if isinstance(checks, list):
            lines.extend(
                f"  {c['health']:<8} {c['check']:<20} {c['detail']}"
                for c in checks
            )
        return emit_result(
            Result("health", int(code), payload), args.json, "\n".join(lines)
        )

    return guard(run, args, "health")


def cmd_archive(args: argparse.Namespace) -> int:
    def run() -> int:
        outcome = commands.do_archive(
            args.port, Path(args.output) if args.output else None, args.baud
        )
        human = outcome.rendered
        if outcome.directory:
            human += f"\n\nWritten: {outcome.directory}"
        return emit_result(
            Result(
                "archive",
                int(ExitCode.SUCCESS),
                {
                    "status": outcome.status,
                    "gaps": list(outcome.gaps),
                    "directory": str(outcome.directory) if outcome.directory else None,
                },
            ),
            args.json,
            human,
        )

    return guard(run, args, "archive")


def cmd_reset(args: argparse.Namespace) -> int:
    def run() -> int:
        rendered, code = commands.do_reset(
            args.port,
            baud=args.baud,
            confirm=args.confirm,
            accept_config_loss=args.accept_config_loss,
            archive_to=Path(args.archive_to) if args.archive_to else None,
            platform=args.platform,
            accept_unverified=args.accept_unverified,
        )
        return emit_result(
            Result("reset", int(code), {"confirmed": args.confirm}), args.json, rendered
        )

    return guard(run, args, "reset")


def cmd_recover_access(args: argparse.Namespace) -> int:
    """Full access recovery, including the physical step where one is needed."""

    def run() -> int:
        rendered, code = interactive.do_recover_access_interactive(
            target(args.port),
            baud=args.baud,
            confirm=args.confirm,
            archive_to=Path(args.archive_to) if args.archive_to else None,
            platform=args.platform,
            restore=args.restore,
            accept_unverified=args.accept_unverified,
        )
        return emit_result(
            Result("recover access", int(code), {"confirmed": args.confirm}),
            args.json,
            rendered,
        )

    return guard(run, args, "recover access")


def cmd_resolve(args: argparse.Namespace) -> int:
    def run() -> int:
        outcome = support.do_resolve(target(args.port), rollback=args.rollback)
        return emit_result(
            Result("resolve", int(outcome.code), {"rollback": args.rollback}),
            args.json,
            outcome.rendered,
        )

    return guard(run, args, "resolve")


def register(sub: Subparsers) -> None:
    rescue_parser = sub.add_parser(
        "rescue", help="diagnose, preserve and recommend (read-only)"
    )
    rescue_parser.add_argument("port")
    rescue_parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    rescue_parser.set_defaults(handler=cmd_rescue)

    health_parser = sub.add_parser("health", help="POST, flash and memory checks")
    health_parser.add_argument("port")
    health_parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    health_parser.set_defaults(handler=cmd_health)

    archive_parser = sub.add_parser(
        "archive", help="preserve what the device will disclose"
    )
    archive_parser.add_argument("port")
    archive_parser.add_argument("-o", "--output", help="directory for the bundle")
    archive_parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    archive_parser.set_defaults(handler=cmd_archive)

    reset_parser = sub.add_parser("reset", help="erase to a known-empty baseline")
    reset_parser.add_argument("port")
    reset_parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    reset_parser.add_argument(
        "--confirm", action="store_true", help="actually perform the reset"
    )
    reset_parser.add_argument(
        "--accept-config-loss",
        action="store_true",
        help="proceed even though preservation captured nothing",
    )
    reset_parser.add_argument("--archive-to", help="write the archive bundle here")
    reset_parser.add_argument(
        "--platform",
        choices=("router", "catalyst"),
        help=(
            "state the platform when the device will not identify itself; "
            "reset is irreversible, so it will not guess"
        ),
    )
    reset_parser.add_argument(
        "--accept-unverified",
        action="store_true",
        help=(
            "proceed on a Catalyst family ciscoyoke has no profile for, using "
            "Cisco's generic procedure"
        ),
    )
    reset_parser.set_defaults(handler=cmd_reset)

    recover_parser = sub.add_parser("recover", help="access and image recovery")
    recover_sub = recover_parser.add_subparsers(dest="subcommand", required=True)
    access_parser = recover_sub.add_parser("access", help="password recovery")
    access_parser.add_argument("port")
    access_parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    access_parser.add_argument("--confirm", action="store_true")
    access_parser.add_argument("--archive-to", help="write the archive bundle here")
    access_parser.add_argument(
        "--platform",
        choices=("router", "catalyst"),
        help=(
            "state the platform when a locked device will not identify itself; "
            "refused if it contradicts what the device reports"
        ),
    )
    access_parser.add_argument(
        "--no-restore",
        dest="restore",
        action="store_false",
        help=(
            "leave the previous configuration moved aside rather than loading "
            "it back; follow with reset for a clean device"
        ),
    )
    access_parser.add_argument(
        "--accept-unverified",
        action="store_true",
        help=(
            "proceed on a Catalyst family ciscoyoke has no profile for, using "
            "Cisco's generic procedure"
        ),
    )
    access_parser.set_defaults(handler=cmd_recover_access)
    # `recover image` lives with the rest of image rescue.
    image.register(recover_sub)


def register_resolve(sub: Subparsers) -> None:
    """Separate from :func:`register` only so ``resolve`` keeps its place in ``--help``."""
    resolve = sub.add_parser(
        "resolve", help="reconcile an interrupted recovery against the device"
    )
    resolve.add_argument("port")
    resolve.add_argument(
        "--rollback",
        action="store_true",
        help="compensate what was applied rather than resuming",
    )
    resolve.set_defaults(handler=cmd_resolve)
