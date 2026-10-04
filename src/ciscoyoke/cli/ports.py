"""``ports``, ``ports label`` and ``ports labels``: adapter identity and names."""

from __future__ import annotations

import argparse

from ciscoyoke.cli.common import Subparsers, emit_result, guard
from ciscoyoke.commands import CommandError
from ciscoyoke.result.exits import ExitCode
from ciscoyoke.result.schema import Result
from ciscoyoke.transport import labels
from ciscoyoke.transport.identity import IdentityStrength, enumerate_ports
from ciscoyoke.ui import Style


def cmd_ports(args: argparse.Namespace) -> int:
    identities = enumerate_ports()

    if not identities:
        result = Result(
            "ports",
            int(ExitCode.DEVICE_NOT_FOUND),
            {"ports": []},
            notes=("no serial ports found",),
        )
        return emit_result(
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
    return emit_result(result, args.json, "\n".join(lines))


def cmd_ports_label(args: argparse.Namespace) -> int:
    def run() -> int:
        if args.remove:
            removed = labels.remove(args.name)
            message = (
                f"Removed label {args.name!r}."
                if removed
                else f"No label {args.name!r}."
            )
            return emit_result(
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

        return emit_result(
            Result("ports label", int(ExitCode.SUCCESS), label.to_json()),
            args.json,
            f"Labelled {args.port} as {label.name!r} ({label.adapter}).\n"
            f"  {label.note}",
        )

    return guard(run, args, "ports label")


def cmd_ports_list_labels(args: argparse.Namespace) -> int:
    known = labels.load()
    present = {identity.key for identity in enumerate_ports()}

    if not known:
        return emit_result(
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
    return emit_result(
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


def register(sub: Subparsers) -> None:
    ports_parser = sub.add_parser("ports", help="adapter identity and labels")
    ports_parser.set_defaults(handler=cmd_ports)
    ports_sub = ports_parser.add_subparsers(dest="subcommand")

    label = ports_sub.add_parser("label", help="bind a stable name to an adapter")
    label.add_argument("name")
    label.add_argument("port", nargs="?")
    label.add_argument("--remove", action="store_true")
    label.set_defaults(handler=cmd_ports_label)

    listing = ports_sub.add_parser("labels", help="list assigned labels")
    listing.set_defaults(handler=cmd_ports_list_labels)
