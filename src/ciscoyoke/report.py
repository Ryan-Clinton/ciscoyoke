"""One shareable file from a failed run, built so it can be fixed remotely.

``ciscoyoke report`` packages the most recent run -- or a named one -- into a zip
a maintainer can reproduce the failure from without the hardware:

* the run's transcript, **scrubbed**, and with the output of every command that
  reads a configuration removed before scrubbing even starts;
* the failure record :mod:`ciscoyoke.diagnose` wrote beside it;
* host diagnostics and the adapter inventory from ``support-bundle``;
* a checklist for the two things no recording holds: the model on the label,
  and what the LEDs did.

What is **never** included: archive bundles, stored configurations, or any
transcript that has not been through both passes. The first real reports taught
that a scrubber cannot know a config's banner names a school, so configuration
output is not scrubbed and hoped for -- it is removed.
"""

from __future__ import annotations

import json
import re
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ciscoyoke.diagnose import TAIL_CHARS, failure_path, unrecognised_prompt
from ciscoyoke.journal.paths import transcripts_dir
from ciscoyoke.support import do_support_bundle
from ciscoyoke.transcript.schema import Direction, Record, Transcript, read, write
from ciscoyoke.transcript.scrub import ScrubResult, scrub

ISSUE_URL = (
    "https://github.com/Ryan-Clinton/ciscoyoke/issues/new?template=hardware-report.yml"
)

# Commands whose output is somebody's configuration. Their replies are dropped
# whole, before scrubbing, because scrubbing cannot be trusted with prose.
_CONFIG_READS = re.compile(
    rb"^\s*(?:show\s+(?:run|start|archive|tech|config)\S*|more\s+\S+|"
    rb"cat\s+\S+|type\s+\S+|copy\s+\S+\s+(?:terminal|console):?)",
    re.IGNORECASE,
)

README = """ciscoyoke failure report
=========================

Contents
  transcript.ytx.pub   the run, scrubbed; configuration output removed
  failure.json         what the tool expected, what it saw, the last prompt
  host/                doctor output, versions and the adapter inventory

Removed on purpose
  Any configuration the device printed (show running-config, more flash:...,
  and the like) is cut out entirely, not scrubbed. Archive bundles are never
  included.

Before you share this
  1. Open transcript.ytx.pub and read it. Scrubbing hides passwords, keys,
     addresses, hostnames, serial numbers and MAC addresses, but it cannot know
     everything that identifies you or a previous owner.
  2. Note, for the issue:
       - the model exactly as printed on the label (e.g. WS-C2960-24TT-L)
       - which LEDs did what when you held MODE, and when you let go
       - which console port and cable you used (RJ-45 or USB; cable chipset
         if you know it)

Then attach this zip to a new issue:
  {issue}
"""


@dataclass(frozen=True, slots=True)
class ReportOutcome:
    path: Path
    scrub: ScrubResult
    elided: int
    run: Path
    had_failure: bool

    def render(self) -> str:
        lines = [
            f"Report written to {self.path}",
            "",
            f"  run                {self.run.name}",
            f"  failure record     {'included' if self.had_failure else 'none recorded for this run'}",
            f"  config output      {self.elided} block(s) removed before scrubbing",
            "",
            self.scrub.report(),
            "",
            "Read transcript.ytx.pub inside the zip before sharing it.",
            f"Then attach the zip to a new issue: {ISSUE_URL}",
        ]
        return "\n".join(lines)


def latest_run(directory: Path | None = None) -> Path | None:
    """The most recent raw transcript a destructive command recorded."""
    folder = directory if directory is not None else transcripts_dir()
    runs = sorted(folder.glob("*.ytx"), key=lambda path: path.stat().st_mtime)
    return runs[-1] if runs else None


def elide_config_output(transcript: Transcript) -> tuple[Transcript, int]:
    """Replace every reply to a configuration-reading command with a marker.

    From the TX record that asks for a configuration up to the next TX, all RX
    is dropped and one marker record says how much went. The marker keeps the
    exchange legible -- a maintainer can see the command was answered -- while
    carrying none of the answer.
    """
    records: list[Record] = []
    eliding = False
    removed = 0
    elided_blocks = 0
    first_at = 0.0

    def close() -> None:
        nonlocal removed
        records.append(
            Record(
                first_at,
                Direction.RX,
                f"\r\n<configuration output removed from report: {removed} bytes>\r\n".encode(),
            )
        )
        removed = 0

    for record in transcript:
        if record.direction is Direction.TX:
            if eliding:
                close()
            eliding = bool(_CONFIG_READS.match(record.data)) and not record.secret
            if eliding:
                elided_blocks += 1
                first_at = record.t
            records.append(record)
            continue
        if eliding:
            removed += len(record.data)
            continue
        records.append(record)
    if eliding:
        close()

    return (
        Transcript(
            records=tuple(records),
            public=transcript.public,
            scrub_version=transcript.scrub_version,
            source=transcript.source,
        ),
        elided_blocks,
    )


def _safe_failure(path: Path, shareable: Transcript) -> dict[str, Any]:
    """The failure record, with its stream excerpts rebuilt from the safe copy.

    The record was written at failure time from the raw stream, so its
    ``last_output`` can hold configuration text the report has just removed.
    Both excerpts are recomputed from the elided, scrubbed transcript instead.
    """
    record: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    text = "".join(
        r.data.decode("latin-1") for r in shareable if r.direction is Direction.RX
    )
    record["last_output"] = text[-TAIL_CHARS:]
    record["unrecognised_prompt"] = unrecognised_prompt(text)
    record["transcript"] = "transcript.ytx.pub"
    return record


def do_report(run: Path | None = None, output: Path | None = None) -> ReportOutcome:
    """Build the report zip for ``run`` (default: the most recent run)."""
    chosen = run if run is not None else latest_run()
    if chosen is None:
        raise FileNotFoundError(
            "no recorded runs found; destructive commands record to "
            f"{transcripts_dir()}, or pass --run with a transcript"
        )

    raw = read(chosen)
    elided, blocks = elide_config_output(raw)
    scrubbed = scrub(elided, source=f"report: {chosen.name}")

    stamp = time.strftime("%Y%m%d-%H%M%S")
    destination = (output if output is not None else Path.cwd()) / (
        f"ciscoyoke-report-{stamp}.zip"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)

    failure = failure_path(chosen)
    with tempfile.TemporaryDirectory() as scratch:
        staging = Path(scratch)
        write(staging / "transcript.ytx.pub", scrubbed.transcript)
        if failure.exists():
            (staging / "failure.json").write_text(
                json.dumps(_safe_failure(failure, scrubbed.transcript), indent=2),
                encoding="utf-8",
            )
        do_support_bundle(staging / "host")
        # The bundle's own README says it contains no transcript, which is true
        # of a bundle and false of this report; the report's README replaces it.
        (staging / "host" / "README.txt").unlink(missing_ok=True)
        (staging / "README.txt").write_text(
            README.format(issue=ISSUE_URL), encoding="utf-8"
        )
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(staging.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(staging).as_posix())

    return ReportOutcome(
        path=destination,
        scrub=scrubbed,
        elided=blocks,
        run=chosen,
        had_failure=failure.exists(),
    )
