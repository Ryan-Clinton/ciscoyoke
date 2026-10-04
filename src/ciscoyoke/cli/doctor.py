"""``doctor``: host serial and capability checks."""

from __future__ import annotations

import argparse
import sys

from ciscoyoke import doctor as host
from ciscoyoke.cli.common import Subparsers, emit_result
from ciscoyoke.result.exits import ExitCode
from ciscoyoke.result.schema import Result
from ciscoyoke.ui import Style


def cmd_doctor(args: argparse.Namespace) -> int:
    diagnosis = host.run()

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
            # A Linux capability. On a Mac runner this line was advice for a
            # mechanism macOS does not have.
            *(
                ["  - grant bind-service capability to an approved helper"]
                if sys.platform.startswith("linux")
                else []
            ),
            "  - use an external TFTP server   --tftp external://HOST",
            "  - use XMODEM where supported    --via xmodem",
        ]

    code = ExitCode.SUCCESS
    result = Result("doctor", int(code), diagnosis.to_json())
    return emit_result(result, args.json, "\n".join(lines))


def register(sub: Subparsers) -> None:
    doctor_parser = sub.add_parser("doctor", help="host serial and capability checks")
    doctor_parser.set_defaults(handler=cmd_doctor)
