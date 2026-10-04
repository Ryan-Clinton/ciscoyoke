"""``transcript scrub`` and ``transcript replay``."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ciscoyoke.cli.common import Subparsers, emit_result
from ciscoyoke.result.exits import ExitCode
from ciscoyoke.result.schema import Result
from ciscoyoke.transcript import replay as replay_module
from ciscoyoke.transcript import schema as transcript_schema
from ciscoyoke.transcript import scrub as scrub_module


def cmd_transcript_scrub(args: argparse.Namespace) -> int:
    source = Path(args.path)
    try:
        transcript = transcript_schema.read(source)
    except (OSError, transcript_schema.TranscriptFormatError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return int(ExitCode.DEVICE_NOT_FOUND)

    provenance = args.source or source.name
    if not provenance.startswith(("hardware:", "synthetic:")):
        print(
            "note: --source should begin 'hardware:' or 'synthetic:'; the README "
            "support matrix derives its marks from that prefix",
            file=sys.stderr,
        )
    outcome = scrub_module.scrub(transcript, source=provenance)
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
    return emit_result(result, args.json, f"{outcome.report()}\n\nWritten: {destination}")


def cmd_transcript_replay(args: argparse.Namespace) -> int:
    """Show what the tracker concludes from a recording, event by event."""
    try:
        transcript = transcript_schema.read(Path(args.path))
    except (OSError, transcript_schema.TranscriptFormatError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return int(ExitCode.DEVICE_NOT_FOUND)

    outcome = replay_module.replay(transcript)
    result = Result("transcript replay", int(ExitCode.SUCCESS), outcome.to_json())
    return emit_result(result, args.json, outcome.render())


def register(sub: Subparsers) -> None:
    transcript_parser = sub.add_parser("transcript", help="transcript utilities")
    transcript_sub = transcript_parser.add_subparsers(dest="subcommand", required=True)

    scrub_parser = transcript_sub.add_parser(
        "scrub", help="produce a shareable transcript from a raw one"
    )
    scrub_parser.add_argument("path", help="raw .ytx transcript")
    scrub_parser.add_argument("-o", "--output", help="destination (default: <path>.pub)")
    scrub_parser.add_argument(
        "--source",
        help=(
            "provenance, e.g. 'hardware: WS-C2950G-24-EI, recover access'; "
            "defaults to the input filename"
        ),
    )
    scrub_parser.set_defaults(handler=cmd_transcript_scrub)

    replay_parser = transcript_sub.add_parser(
        "replay",
        help="show every state the tracker concludes from a recording, and where it stalls",
    )
    replay_parser.add_argument("path", help=".ytx or .ytx.pub transcript")
    replay_parser.set_defaults(handler=cmd_transcript_replay)
