"""``recover image``: user-supplied image rescue."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ciscoyoke import imaging
from ciscoyoke.cli.common import Subparsers, emit_result, guard, target
from ciscoyoke.playbook.transfer import Progress
from ciscoyoke.result.schema import Result
from ciscoyoke.transport.serial_ import DEFAULT_BAUD


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
        return emit_result(
            Result("recover image", int(code), payload), args.json, rendered
        )

    return guard(run, args, "recover image")


def register(recover_sub: Subparsers) -> None:
    """Add ``image`` under the ``recover`` command."""
    image = recover_sub.add_parser("image", help="user-supplied image rescue")
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
