"""Argument wiring for the commands that were added after the first CLI pass.

Kept separate from :mod:`ciscoyoke.cli` so the parser definition stays readable:
one module holds the original verbs, this one holds image rescue, the lab verbs,
labels, the interactive console, the support bundle and interrupted-run
resolution. Both register into the same subparser.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from pathlib import Path

from ciscoyoke import imaging, interactive, labs, support
from ciscoyoke.commands import CommandError
from ciscoyoke.playbook.transfer import Progress
from ciscoyoke.result.exits import ExitCode
from ciscoyoke.result.schema import Result
from ciscoyoke.transport import labels
from ciscoyoke.transport.identity import enumerate_ports
from ciscoyoke.transport.serial_ import DEFAULT_BAUD


def target(value: str) -> str:
    """Accept a label anywhere a port name is accepted.

    The point of labels is that ``rescue bench-left`` works; resolving here
    means every command gets that without each one knowing about labels.
    """
    return labels.resolve_target(value)


def _emit(result: Result, as_json: bool, human: str) -> int:
    from ciscoyoke.ui import emit

    if as_json:
        print(result.render())
    else:
        emit(human)
    return result.exit_code


def _guard(
    handler: Callable[[], int], args: argparse.Namespace, command: str
) -> int:
    """Run a command, turning its failure into the documented exit code."""
    try:
        return handler()
    except CommandError as exc:
        if args.json:
            print(Result(command, int(exc.code), {"error": str(exc)}).render())
        else:
            print(f"error: {exc}", file=sys.stderr)
        return int(exc.code)


def _progress_line(progress: Progress) -> None:
    """Render live transfer progress to stderr.

    stderr specifically: under --json, stdout has to stay pure JSON or it stops
    being machine-readable. A transfer that can take ninety minutes still needs
    to show signs of life either way.
    """
    print(f"\r{progress.render()}", end="", file=sys.stderr, flush=True)


def cmd_recover_image(args: argparse.Namespace) -> int:
    def run() -> int:
        reporter = None if args.quiet else _progress_line
        rendered, code = imaging.do_recover_image(
            target(args.port),
            Path(args.image),
            baud=args.baud,
            confirm=args.confirm,
            accept_stranded_risk=args.accept_stranded_risk,
            via=args.via,
            on_progress=reporter,
        )
        if reporter is not None:
            print(file=sys.stderr)

        payload: dict[str, object] = {"confirmed": args.confirm}
        payload.update(imaging.last_result())
        return _emit(
            Result("recover image", int(code), payload), args.json, rendered
        )

    return _guard(run, args, "recover image")


def cmd_lab_verify(args: argparse.Namespace) -> int:
    def run() -> int:
        rendered, payload, code = labs.do_lab_verify(
            Path(args.labfile), baud=args.baud
        )
        return _emit(Result("lab verify", int(code), payload), args.json, rendered)

    return _guard(run, args, "lab verify")


def cmd_lab_apply(args: argparse.Namespace) -> int:
    def run() -> int:
        rendered, payload, code = labs.do_lab_apply(
            Path(args.labfile),
            baud=args.baud,
            confirm=args.confirm,
            allow_weak=args.allow_weak_match,
        )
        return _emit(Result("lab apply", int(code), payload), args.json, rendered)

    return _guard(run, args, "lab apply")


def cmd_ports_label(args: argparse.Namespace) -> int:
    def run() -> int:
        if args.remove:
            removed = labels.remove(args.name)
            message = (
                f"Removed label {args.name!r}."
                if removed
                else f"No label {args.name!r}."
            )
            return _emit(
                Result(
                    "ports label",
                    int(ExitCode.SUCCESS if removed else ExitCode.DEVICE_NOT_FOUND),
                    {"name": args.name, "removed": removed},
                ),
                args.json,
                message,
            )

        if not args.port:
            raise CommandError(
                "a port is required unless --remove is given",
                ExitCode.DEVICE_NOT_FOUND,
            )

        try:
            label = labels.assign(args.name, args.port)
        except labels.LabelRefusedError as exc:
            raise CommandError(str(exc), ExitCode.STATE_UNCERTAIN) from exc

        return _emit(
            Result("ports label", int(ExitCode.SUCCESS), label.to_json()),
            args.json,
            f"Labelled {args.port} as {label.name!r} ({label.adapter}).\n"
            f"  {label.note}",
        )

    return _guard(run, args, "ports label")


def cmd_ports_list_labels(args: argparse.Namespace) -> int:
    known = labels.load()
    present = {identity.key for identity in enumerate_ports()}

    if not known:
        return _emit(
            Result("ports labels", int(ExitCode.SUCCESS), {"labels": []}),
            args.json,
            "No labels assigned. Assign one with: ciscoyoke ports label NAME PORT",
        )

    lines = ["", f"{'LABEL':<16} {'ADAPTER':<14} PRESENT"]
    lines.extend(
        f"{label.name:<16} {label.adapter:<14} "
        f"{'yes' if label.key in present else 'no'}"
        for label in known.values()
    )
    return _emit(
        Result(
            "ports labels",
            int(ExitCode.SUCCESS),
            {
                "labels": [
                    {**label.to_json(), "present": label.key in present}
                    for label in known.values()
                ]
            },
        ),
        args.json,
        "\n".join(lines),
    )


def cmd_console(args: argparse.Namespace) -> int:
    def run() -> int:
        return support.do_console(
            target(args.port),
            args.baud,
            Path(args.record) if args.record else None,
        )

    return _guard(run, args, "console")


def cmd_support_bundle(args: argparse.Namespace) -> int:
    def run() -> int:
        summary, directory = support.do_support_bundle(
            Path(args.output), target(args.port) if args.port else None
        )
        return _emit(
            Result(
                "support-bundle",
                int(ExitCode.SUCCESS),
                {"directory": str(directory)},
            ),
            args.json,
            summary,
        )

    return _guard(run, args, "support-bundle")


def cmd_resolve(args: argparse.Namespace) -> int:
    def run() -> int:
        outcome = support.do_resolve(target(args.port), rollback=args.rollback)
        return _emit(
            Result("resolve", int(outcome.code), {"rollback": args.rollback}),
            args.json,
            outcome.rendered,
        )

    return _guard(run, args, "resolve")


def cmd_recover_access_interactive(args: argparse.Namespace) -> int:
    """Full access recovery, including the physical step where one is needed."""

    def run() -> int:
        rendered, code = interactive.do_recover_access_interactive(
            target(args.port),
            baud=args.baud,
            confirm=args.confirm,
            archive_to=Path(args.archive_to) if args.archive_to else None,
        )
        return _emit(
            Result("recover access", int(code), {"confirmed": args.confirm}),
            args.json,
            rendered,
        )

    return _guard(run, args, "recover access")


def cmd_login(args: argparse.Namespace) -> int:
    """Identify a locked device using credentials that are simply known."""

    def run() -> int:
        rendered, code = interactive.do_authenticated_intake(
            target(args.port),
            baud=args.baud,
            password_stdin=args.password_stdin,
        )
        return _emit(Result("login", int(code), {}), args.json, rendered)

    return _guard(run, args, "login")


def register(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Add every later verb to an existing subparser."""
    console = sub.add_parser("console", help="interactive console with recording")
    console.add_argument("port")
    console.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    console.add_argument("--record", help="write a raw transcript here")
    console.set_defaults(handler=cmd_console)

    bundle = sub.add_parser(
        "support-bundle", help="collect diagnostics for a bug report"
    )
    bundle.add_argument("-o", "--output", default="ciscoyoke-support")
    bundle.add_argument("--port", help="include any unfinished journal for this port")
    bundle.set_defaults(handler=cmd_support_bundle)

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

    login = sub.add_parser(
        "login", help="identify a locked device using known credentials"
    )
    login.add_argument("port")
    login.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    login.add_argument(
        "--password-stdin",
        action="store_true",
        help="read the password from stdin instead of prompting",
    )
    login.set_defaults(handler=cmd_login)


def register_labels(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    """Add the ``ports label`` and ``ports labels`` verbs."""
    label = sub.add_parser("label", help="bind a stable name to an adapter")
    label.add_argument("name")
    label.add_argument("port", nargs="?")
    label.add_argument("--remove", action="store_true")
    label.set_defaults(handler=cmd_ports_label)

    listing = sub.add_parser("labels", help="list assigned labels")
    listing.set_defaults(handler=cmd_ports_list_labels)


def register_recover_image(
    sub: argparse._SubParsersAction[argparse.ArgumentParser],
) -> None:
    image = sub.add_parser("image", help="user-supplied image rescue")
    image.add_argument("port")
    image.add_argument("--image", required=True, help="local path to an IOS image")
    image.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    image.add_argument("--confirm", action="store_true")
    image.add_argument(
        "--via", choices=("xmodem", "tftp"), default="xmodem"
    )
    image.add_argument(
        "--quiet", action="store_true", help="suppress live progress on stderr"
    )
    image.add_argument(
        "--accept-stranded-risk",
        action="store_true",
        help="delete the only bootable image to make room (leaves no rollback)",
    )
    image.set_defaults(handler=cmd_recover_image)


def register_lab(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    verify = sub.add_parser("verify", help="check cabling against a lab file")
    verify.add_argument("labfile")
    verify.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    verify.set_defaults(handler=cmd_lab_verify)

    apply_parser = sub.add_parser("apply", help="push configuration to the lab")
    apply_parser.add_argument("labfile")
    apply_parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    apply_parser.add_argument("--confirm", action="store_true")
    apply_parser.add_argument(
        "--allow-weak-match",
        action="store_true",
        help="write to devices matched only by port name",
    )
    apply_parser.set_defaults(handler=cmd_lab_apply)
