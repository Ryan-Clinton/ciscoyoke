"""Command-line interface.

Every command supports ``--json`` and returns a documented exit code, so this
is usable from a shell script or CI as well as by a person.

Commands divide sharply. ``doctor``, ``ports``, ``scan``, ``intake``, ``rescue``,
``health`` and ``archive`` change nothing, so they are safe to point at hardware
in unknown condition. ``reset`` and ``recover`` can destroy things, so they take
a transport lease, journal every mutation, dry-run by default, and require
``--confirm`` before a single byte is sent.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

from ciscoyoke import __version__, commands, doctor
from ciscoyoke.identify.probe import Intake, intake
from ciscoyoke.result.exits import ExitCode
from ciscoyoke.result.schema import Result
from ciscoyoke.session import Session
from ciscoyoke.topology import labfile
from ciscoyoke.transcript import schema as transcript_schema
from ciscoyoke.transcript import scrub as scrub_module
from ciscoyoke.transport.identity import IdentityStrength, enumerate_ports
from ciscoyoke.transport.serial_ import (
    CANDIDATE_BAUDS,
    DEFAULT_BAUD,
    SerialTransport,
    SerialUnavailableError,
)
from ciscoyoke.ui import Style, emit


def _print(result: Result, as_json: bool, human: str) -> int:
    if as_json:
        # JSON is always ASCII-safe: non-ASCII is escaped by json.dumps, so a
        # legacy console cannot break machine-readable output.
        print(result.render())
    else:
        emit(human)
    return result.exit_code


def cmd_doctor(args: argparse.Namespace) -> int:
    diagnosis = doctor.run()

    style = Style()
    lines = ["Host diagnostics", ""]
    lines.extend(
        f"  {style.mark(check.status.value)} {check.name:20} {check.detail}"
        for check in diagnosis.checks
    )

    if not diagnosis.embedded_tftp_available:
        lines += [
            "",
            "Embedded TFTP unavailable.",
            "",
            "Options:",
            "  - grant bind-service capability to an approved helper",
            "  - use an external TFTP server   --tftp external://HOST",
            "  - use XMODEM where supported    --via xmodem",
        ]

    code = ExitCode.SUCCESS
    result = Result("doctor", int(code), diagnosis.to_json())
    return _print(result, args.json, "\n".join(lines))


def cmd_ports(args: argparse.Namespace) -> int:
    identities = enumerate_ports()

    if not identities:
        result = Result(
            "ports",
            int(ExitCode.DEVICE_NOT_FOUND),
            {"ports": []},
            notes=("no serial ports found",),
        )
        return _print(
            result,
            args.json,
            "No serial ports found. Check the adapter is plugged in, "
            "then run: ciscoyoke doctor",
        )

    style = Style()
    blank = style.dash
    header = f"{'PORT':<12} {'ADAPTER':<14} {'USB SERIAL':<16} {'LOCATION':<12} IDENTITY"
    lines = ["", header]
    lines.extend(
        f"{identity.device:<12} "
        f"{identity.adapter:<14} "
        f"{identity.serial_number or blank:<16} "
        f"{identity.location or blank:<12} "
        f"{identity.strength.value}"
        for identity in identities
    )

    weak = [i for i in identities if i.strength is not IdentityStrength.SERIAL]
    if weak:
        lines += [
            "",
            "Ports without a USB serial number cannot be recognised again after",
            "replugging into a different socket. Their identity falls back to",
            "physical location, which is stated above rather than assumed.",
        ]

    result = Result(
        "ports",
        int(ExitCode.SUCCESS),
        {
            "ports": [
                {
                    "device": i.device,
                    "adapter": i.adapter,
                    "vid": i.vid,
                    "pid": i.pid,
                    "serial_number": i.serial_number,
                    "location": i.location,
                    "identity_strength": i.strength.value,
                    "key": i.key,
                }
                for i in identities
            ]
        },
    )
    return _print(result, args.json, "\n".join(lines))


def cmd_transcript_scrub(args: argparse.Namespace) -> int:
    source = Path(args.path)
    try:
        transcript = transcript_schema.read(source)
    except (OSError, transcript_schema.TranscriptFormatError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return int(ExitCode.DEVICE_NOT_FOUND)

    outcome = scrub_module.scrub(transcript, source=source.name)
    destination = (
        Path(args.output)
        if args.output
        else source.with_suffix(source.suffix + ".pub")
    )
    transcript_schema.write(destination, outcome.transcript)

    result = Result(
        "transcript scrub",
        int(ExitCode.SUCCESS),
        {
            "source": str(source),
            "output": str(destination),
            "credentials_redacted": outcome.credentials,
            "addresses_anonymised": outcome.addresses,
            "hostnames_anonymised": outcome.hostnames,
            "key_blocks_removed": outcome.key_blocks,
        },
        notes=(
            "automated scrubbing cannot prove a transcript contains no "
            "sensitive data; review before publishing",
        ),
    )
    return _print(result, args.json, f"{outcome.report()}\n\nWritten: {destination}")


def _open_session(port: str, baud: int) -> tuple[Session, SerialTransport]:
    transport = SerialTransport(port, baud=baud).open()
    return Session(transport), transport


def _intake_one(port: str, baud: int) -> Intake:
    session, transport = _open_session(port, baud)
    try:
        return intake(session)
    finally:
        transport.close()


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
    return _print(Result("intake", int(code), payload), args.json, "\n".join(lines))


def cmd_scan(args: argparse.Namespace) -> int:
    """Identify every attached device, passively.

    Runs the same intake against each port in turn. Nothing here writes
    configuration, so it is safe against hardware in unknown condition -- which
    is what makes it a reasonable first command rather than a risky one.
    """
    identities = enumerate_ports()
    if not identities:
        return _print(
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
                f"{'unavailable':<13} {blank:<14} {blank:<8} {blank}"
            )
            advice.append(f"{identity.device}  {exc}")
            continue

        entry.update(result.to_json())
        entry["baud"] = args.baud
        devices.append(entry)

        rows.append(
            f"{identity.device:<10} {identity.adapter:<10} "
            f"{result.observation.state.value:<13} "
            f"{result.facts.model.value or blank:<14} "
            f"{args.baud:<8} {result.observation.confidence.value}"
        )
        if result.note:
            advice.append(f"{identity.device}  {result.note}")

    header = (
        f"{'PORT':<10} {'ADAPTER':<10} {'STATE':<13} "
        f"{'MODEL':<14} {'BAUD':<8} CONFIDENCE"
    )
    lines = ["", f"{len(identities)} endpoints found", "", header, *rows]
    if advice:
        lines += ["", *advice]

    return _print(
        Result("scan", int(ExitCode.SUCCESS), {"devices": devices}),
        args.json,
        "\n".join(lines),
    )


def _run(
    handler: Callable[[], int], args: argparse.Namespace, command: str
) -> int:
    """Call a command implementation, mapping its failure to an exit code."""
    try:
        return handler()
    except commands.CommandError as exc:
        if args.json:
            print(Result(command, int(exc.code), {"error": str(exc)}).render())
        else:
            print(f"error: {exc}", file=sys.stderr)
        return int(exc.code)


def cmd_rescue(args: argparse.Namespace) -> int:
    def run() -> int:
        report = commands.do_rescue(args.port, args.baud)
        return _print(
            Result("rescue", int(ExitCode.SUCCESS), report.to_json()),
            args.json,
            report.render(),
        )

    return _run(run, args, "rescue")


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
        return _print(
            Result("health", int(code), payload), args.json, "\n".join(lines)
        )

    return _run(run, args, "health")


def cmd_archive(args: argparse.Namespace) -> int:
    def run() -> int:
        outcome = commands.do_archive(
            args.port, Path(args.output) if args.output else None, args.baud
        )
        human = outcome.rendered
        if outcome.directory:
            human += f"\n\nWritten: {outcome.directory}"
        return _print(
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

    return _run(run, args, "archive")


def cmd_reset(args: argparse.Namespace) -> int:
    def run() -> int:
        rendered, code = commands.do_reset(
            args.port,
            baud=args.baud,
            confirm=args.confirm,
            accept_config_loss=args.accept_config_loss,
            archive_to=Path(args.archive_to) if args.archive_to else None,
        )
        return _print(
            Result("reset", int(code), {"confirmed": args.confirm}), args.json, rendered
        )

    return _run(run, args, "reset")


def cmd_recover_access(args: argparse.Namespace) -> int:
    def run() -> int:
        rendered, code = commands.do_recover_access(
            args.port, baud=args.baud, confirm=args.confirm
        )
        return _print(
            Result("recover access", int(code), {"confirmed": args.confirm}),
            args.json,
            rendered,
        )

    return _run(run, args, "recover access")


def cmd_lab_verify(args: argparse.Namespace) -> int:
    lab = labfile.load(Path(args.labfile))
    # Nothing is collected from devices yet, so the honest output is the
    # declaration itself plus a statement that it was not checked.
    lines = [f"Lab: {lab.name}", ""]
    lines.extend(f"  declared  {link}" for link in lab.cabling)
    lines += [
        "",
        f"{len(lab.cabling)} link(s) declared across {len(lab.devices)} device(s).",
        "CDP collection is not yet wired to the CLI, so nothing was verified.",
    ]
    return _print(
        Result(
            "lab verify",
            int(ExitCode.STATE_UNCERTAIN),
            {
                "lab": lab.name,
                "devices": [d.name for d in lab.devices],
                "declared_links": [str(link) for link in lab.cabling],
                "verified": False,
            },
        ),
        args.json,
        "\n".join(lines),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ciscoyoke",
        description="A rescue bench for old Cisco hardware.",
    )
    parser.add_argument("--version", action="version", version=f"ciscoyoke {__version__}")
    parser.add_argument(
        "--json", action="store_true", help="emit a versioned machine-readable result"
    )

    sub = parser.add_subparsers(dest="command", required=True)

    doctor_parser = sub.add_parser("doctor", help="host serial and capability checks")
    doctor_parser.set_defaults(handler=cmd_doctor)

    ports_parser = sub.add_parser("ports", help="adapter identity and stability")
    ports_parser.set_defaults(handler=cmd_ports)

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
    reset_parser.set_defaults(handler=cmd_reset)

    recover_parser = sub.add_parser("recover", help="access and image recovery")
    recover_sub = recover_parser.add_subparsers(dest="subcommand", required=True)
    access_parser = recover_sub.add_parser("access", help="password recovery")
    access_parser.add_argument("port")
    access_parser.add_argument("--baud", type=int, default=DEFAULT_BAUD)
    access_parser.add_argument("--confirm", action="store_true")
    access_parser.set_defaults(handler=cmd_recover_access)

    lab_parser = sub.add_parser("lab", help="physical lab topology")
    lab_sub = lab_parser.add_subparsers(dest="subcommand", required=True)
    verify_parser = lab_sub.add_parser("verify", help="check cabling against a lab file")
    verify_parser.add_argument("labfile")
    verify_parser.set_defaults(handler=cmd_lab_verify)

    transcript_parser = sub.add_parser("transcript", help="transcript utilities")
    transcript_sub = transcript_parser.add_subparsers(dest="subcommand", required=True)
    scrub_parser = transcript_sub.add_parser(
        "scrub", help="produce a shareable transcript from a raw one"
    )
    scrub_parser.add_argument("path", help="raw .ytx transcript")
    scrub_parser.add_argument("-o", "--output", help="destination (default: <path>.pub)")
    scrub_parser.set_defaults(handler=cmd_transcript_scrub)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = args.handler
    return int(handler(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
