"""Assembling the parser: the global options, then each module's commands.

The order here is the order of ``ciscoyoke --help``.
"""

from __future__ import annotations

import argparse

from ciscoyoke import __version__
from ciscoyoke.cli import (
    capture,
    doctor,
    identify,
    lab,
    ports,
    recovery,
    support,
    transcript,
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

    doctor.register(sub)
    ports.register(sub)
    identify.register(sub)
    recovery.register(sub)
    lab.register(sub)
    transcript.register(sub)
    capture.register(sub)
    support.register(sub)
    recovery.register_resolve(sub)
    identify.register_login(sub)

    return parser
