"""``scan``, ``intake`` and ``login``: finding out what a device is."""

from __future__ import annotations

import argparse
import sys

from ciscoyoke import commands, interactive
from ciscoyoke.cli.common import Subparsers, emit_result, guard, target
from ciscoyoke.identify.probe import Intake, intake
from ciscoyoke.lifecycle.rescue import next_step
from ciscoyoke.result.exits import ExitCode
from ciscoyoke.result.schema import Result
from ciscoyoke.session import Session
from ciscoyoke.transport.identity import enumerate_ports
from ciscoyoke.transport.serial_ import (
    CANDIDATE_BAUDS,
    DEFAULT_BAUD,
    SerialTransport,
    SerialUnavailableError,
)
from ciscoyoke.ui import Style


def _open_session(port: str, baud: int) -> tuple[Session, SerialTransport]:
    transport = SerialTransport(port, baud=baud).open()
    return Session(transport), transport


def _intake_one(port: str, baud: int) -> Intake:
    session, transport = _open_session(port, baud)
    try:
        result = intake(session)
    finally:
        transport.close()
    commands.note_identity(port, result.facts, "intake")
    return result


def cmd_intake(args: argparse.Namespace) -> int:
    try:
        result = _intake_one(args.port, args.baud)
    except SerialUnavailableError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return int(ExitCode.DEVICE_NOT_FOUND)

    facts = result.facts
    lines = [
        "",
        f"State:        {result.observation.state.value} "
        f"({result.observation.basis.value}/{result.observation.confidence.value})",
        f"Model:        {facts.model.value or 'unknown'}",
        f"IOS version:  {facts.ios_version.value or 'unknown'}",
        f"Serial:       {facts.serial_number.value or 'unknown'}",
        f"Config reg:   {facts.config_register.value or 'unknown'}",
        "",
        result.note,
        "",
        "No destructive action taken.",
    ]

    code = ExitCode.SUCCESS
    if result.locked:
        code = ExitCode.AUTHENTICATION_REQUIRED
    elif not result.facts.identified:
        code = ExitCode.STATE_UNCERTAIN

    payload = dict(result.to_json())
    payload["port"] = args.port
    payload["baud"] = args.baud
    return emit_result(Result("intake", int(code), payload), args.json, "\n".join(lines))


def cmd_scan(args: argparse.Namespace) -> int:
    """Identify every attached device, passively.

    Runs the same intake against each port in turn. Nothing here writes
    configuration, so it is safe against hardware in unknown condition -- which
    is what makes it a reasonable first command rather than a risky one.
    """
    identities = enumerate_ports()
    if not identities:
        return emit_result(
            Result("scan", int(ExitCode.DEVICE_NOT_FOUND), {"devices": []}),
            args.json,
            "No serial ports found. Check the adapter is plugged in, "
            "then run: ciscoyoke doctor",
        )

    style = Style()
    blank = style.dash
    devices: list[dict[str, object]] = []
    rows: list[str] = []
    advice: list[str] = []

    for identity in identities:
        entry: dict[str, object] = {
            "port": identity.device,
            "adapter": identity.adapter,
            "identity_strength": identity.strength.value,
        }
        try:
            result = _intake_one(identity.device, args.baud)
        except SerialUnavailableError as exc:
            entry["error"] = str(exc)
            devices.append(entry)
            rows.append(
                f"{identity.device:<10} {identity.adapter:<10} "
                f"{'unavailable':<13} {blank:<16} {blank:<6} {blank:<10} see below"
            )
            advice.append(f"{identity.device}  {exc}")
            continue

        entry.update(result.to_json())
        entry["baud"] = args.baud
        devices.append(entry)

        rows.append(
            f"{identity.device:<10} {identity.adapter:<10} "
            f"{result.observation.state.value:<13} "
            f"{result.facts.model.value or blank:<16} "
            f"{args.baud:<6} {result.observation.confidence.value:<10} "
            f"{next_step(result.observation.state)}"
        )
        entry["next"] = next_step(result.observation.state)
        if result.note:
            advice.append(f"{identity.device}  {result.note}")

    header = (
        f"{'PORT':<10} {'ADAPTER':<10} {'STATE':<13} "
        f"{'MODEL':<16} {'BAUD':<6} {'CONFIDENCE':<10} NEXT"
    )
    lines = ["", f"{len(identities)} endpoints found", "", header, *rows]
    if advice:
        lines += ["", *advice]

    return emit_result(
        Result("scan", int(ExitCode.SUCCESS), {"devices": devices}),
        args.json,
        "\n".join(lines),
    )


def cmd_login(args: argparse.Namespace) -> int:
    """Identify a locked device using credentials that are simply known."""

    def run() -> int:
        rendered, code = interactive.do_authenticated_intake(
            target(args.port),
            baud=args.baud,
            password_stdin=args.password_stdin,
        )
        return emit_result(Result("login", int(code), {}), args.json, rendered)

    return guard(run, args, "login")


def register(sub: Subparsers) -> None:
    scan_parser = sub.add_parser(
        "scan", help="identify every attached device (read-only)"
    )
    scan_parser.add_argument(
        "--baud", type=int, default=DEFAULT_BAUD,
        help=f"line speed (default {DEFAULT_BAUD}; candidates {CANDIDATE_BAUDS})",
    )
    scan_parser.set_defaults(handler=cmd_scan)

    intake_parser = sub.add_parser(
        "intake", help="identify one device without changing it"
    )
    intake_parser.add_argument("port", help="serial port, e.g. COM3 or /dev/ttyUSB0")
    intake_parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    intake_parser.set_defaults(handler=cmd_intake)


def register_login(sub: Subparsers) -> None:
    """Separate from :func:`register` only so ``login`` keeps its place in ``--help``."""
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
