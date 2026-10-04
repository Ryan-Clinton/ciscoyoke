"""Command-line interface.

Every command supports ``--json`` and returns a documented exit code, so this
is usable from a shell script or CI as well as by a person.

Commands divide sharply. ``doctor``, ``ports``, ``scan``, ``intake``, ``rescue``,
``health`` and ``archive`` change nothing, so they are safe to point at hardware
in unknown condition. ``reset`` and ``recover`` can destroy things, so they take
a transport lease, journal every mutation, dry-run by default, and require
``--confirm`` before a single byte is sent.

One module per group of commands. Each holds the argument definitions for its
commands and the thin ``cmd_*`` handlers that call the real work elsewhere;
:mod:`ciscoyoke.cli.parser` assembles them.

==============  ==========================================================
``doctor``      ``doctor``
``ports``       ``ports``, ``ports label``, ``ports labels``
``identify``    ``scan``, ``intake``, ``login``
``recovery``    ``rescue``, ``health``, ``archive``, ``reset``,
                ``recover access``, ``resolve``
``image``       ``recover image``
``lab``         ``lab verify``, ``lab apply``
``transcript``  ``transcript scrub``, ``transcript replay``
``capture``     ``console``, ``capture``, ``sweep``
``support``     ``support-bundle``, ``report``
``common``      result printing, error-to-exit-code mapping, label lookup
==============  ==========================================================
"""

from __future__ import annotations

from collections.abc import Sequence

from ciscoyoke.cli.parser import build_parser

__all__ = ["build_parser", "main"]


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    handler = args.handler
    return int(handler(args))
