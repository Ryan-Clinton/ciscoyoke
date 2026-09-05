"""Command-line interface.

Every command supports ``--json`` and returns a documented exit code, so this
is usable from a shell script or CI as well as by a person.

Only the P0 read-only commands are implemented so far -- ``doctor``, ``ports``
and ``transcript scrub``. Nothing here can change a device, which is why none
of it needs the confirmation machinery yet. Commands that will mutate hardware
land with the journal and the lease, not before.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from ciscoyoke import __version__, doctor
from ciscoyoke.identify.probe import Intake, intake
from ciscoyoke.result.exits import ExitCode
from ciscoyoke.result.schema import Result
from ciscoyoke.session import Session
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
