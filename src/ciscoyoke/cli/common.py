"""What every command module shares: printing a result, and failing properly."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from typing import TypeAlias

from ciscoyoke.commands import CommandError
from ciscoyoke.result.schema import Result
from ciscoyoke.transport import labels
from ciscoyoke.ui import emit

#: What ``add_subparsers()`` returns; each module's ``register`` takes one.
Subparsers: TypeAlias = "argparse._SubParsersAction[argparse.ArgumentParser]"


def target(value: str) -> str:
    """Accept a label anywhere a port name is accepted.

    The point of labels is that ``rescue bench-left`` works; resolving here
    means every command gets that without each one knowing about labels.
    """
    return labels.resolve_target(value)


def emit_result(result: Result, as_json: bool, human: str) -> int:
    """Print a result for a person or a program, and return its exit code."""
    if as_json:
        # JSON is always ASCII-safe: non-ASCII is escaped by json.dumps, so a
        # legacy console cannot break machine-readable output.
        print(result.render())
    else:
        emit(human)
    return result.exit_code


def guard(handler: Callable[[], int], args: argparse.Namespace, command: str) -> int:
    """Call a command implementation, mapping its failure to an exit code."""
    try:
        return handler()
    except CommandError as exc:
        if args.json:
            print(Result(command, int(exc.code), {"error": str(exc)}).render())
        else:
            # Through emit, like every other human line: a refused plan carries
            # the same decorations as an accepted one, and a bare print garbled
            # them on a legacy Windows console.
            emit(f"error: {exc}", stream=sys.stderr)
        return int(exc.code)
