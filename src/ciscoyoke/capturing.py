"""The ``capture``, ``sweep`` and interactive ``console`` commands.

Everything here exists to get bytes off unfamiliar hardware and into a file
that the rest of the tool can replay. Tomorrow's first real device is worth
more than any amount of further design, and these are the commands that turn it
into evidence.

``console`` is genuinely duplex, which the previous version was not: it read a
line from the keyboard and only then displayed whatever the device had said, so
a boot happening while you sat at the prompt was invisible until you pressed
Enter. A serial console is not a request/response protocol and cannot be driven
by one.
"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from ciscoyoke.capture import (
    DEFAULT_QUIET_AFTER,
    BaudProbe,
    CaptureStats,
    best_baud,
    capture,
)
from ciscoyoke.commands import CommandError
from ciscoyoke.result.exits import ExitCode
from ciscoyoke.stream.tracker import StateTracker
from ciscoyoke.transcript.writer import TranscriptWriter
from ciscoyoke.transport.serial_ import (
    CANDIDATE_BAUDS,
    DEFAULT_BAUD,
    SerialTransport,
    SerialUnavailableError,
    printable_ratio,
)


def _open(port: str, baud: int) -> SerialTransport:
    try:
        return SerialTransport(port, baud=baud).open()
    except SerialUnavailableError as exc:
        raise CommandError(str(exc), ExitCode.DEVICE_NOT_FOUND) from exc


def _echo(data: bytes) -> None:
    """Show what the device said, without letting a stray byte kill the run.

    Console output is not guaranteed to be clean -- a wrong baud produces
    arbitrary bytes -- so this must never be the thing that ends a capture.
    """
    text = data.decode("latin-1")
    try:
        sys.stdout.write(text)
        sys.stdout.flush()
    except (UnicodeEncodeError, OSError):  # pragma: no cover - terminal dependent
        encoding = getattr(sys.stdout, "encoding", "ascii") or "ascii"
        sys.stdout.write(text.encode(encoding, "replace").decode(encoding))
        sys.stdout.flush()


def do_capture(
    port: str,
    output: Path,
    *,
    baud: int = DEFAULT_BAUD,
    duration: float | None = None,
    quiet_after: float | None = DEFAULT_QUIET_AFTER,
    echo: bool = True,
) -> tuple[str, dict[str, object], ExitCode]:
    """Listen and record. Transmits nothing.

    Safe against a device in any state, which is what makes it the right first
    thing to run against hardware nobody has characterised.
    """
    transport = _open(port, baud)
    tracker = StateTracker()

    print(
        f"Capturing {port} at {baud} baud. Nothing will be transmitted.\n"
        f"Power-cycle the device now to record a cold boot. Ctrl-C to stop.\n",
        file=sys.stderr,
    )

    stats: CaptureStats
    try:
        with TranscriptWriter(output) as writer:
            try:
                stats = capture(
                    transport.read,
                    writer,
                    duration=duration,
                    quiet_after=quiet_after,
                    echo=_echo if echo else None,
                    tracker=tracker,
                )
            except KeyboardInterrupt:
                # Everything so far is already on disk, which is the entire
                # reason the writer flushes per record.
                stats = CaptureStats(
                    bytes_received=sum(1 for _ in ()),
                    records=writer.count,
                    final_state=tracker.current.state,
                )
                print("\n\nStopped.", file=sys.stderr)
    finally:
        transport.close()

    payload: dict[str, object] = {
        "port": port,
        "baud": baud,
        "output": str(output),
        "bytes": stats.bytes_received,
        "records": stats.records,
        "final_state": stats.final_state.value,
        "states_seen": [state.value for state in stats.states_seen],
        "printable_ratio": round(stats.printable_ratio, 3),
        "possible_wrong_baud": stats.looks_like_wrong_baud,
    }
    code = (
        ExitCode.STATE_UNCERTAIN
        if stats.bytes_received == 0 or stats.looks_like_wrong_baud
        else ExitCode.SUCCESS
    )
    return stats.render(output), payload, code


def do_sweep(
    port: str, *, seconds: float = 3.0
) -> tuple[str, dict[str, object], ExitCode]:
    """Try each candidate line speed and report which produced console text.

    Purely passive: it listens at each rate rather than prodding the device.
    On a booting or chattering device this settles the baud question in under
    half a minute; on a silent one it correctly reports that nothing was heard
    at any rate, which is a different and equally useful answer.
    """
    probes: list[BaudProbe] = []

    for baud in CANDIDATE_BAUDS:
        transport = _open(port, baud)
        seen = bytearray()
        deadline = time.monotonic() + seconds
        try:
            while time.monotonic() < deadline:
                seen.extend(transport.read())
                time.sleep(0.02)
        finally:
            transport.close()

        probes.append(
            BaudProbe(
                baud=baud,
                bytes_seen=len(seen),
                printable=printable_ratio(bytes(seen)),
            )
        )

    chosen = best_baud(probes)
    lines = ["", f"Baud sweep on {port} ({seconds:.0f}s per rate)", ""]
    lines.extend(probe.render() for probe in probes)
    lines.append("")

    if chosen is None:
        lines += [
            "  No rate produced anything that looks like console output.",
            "",
            "  That usually means one of:",
            "    - the device is powered off, or has finished booting and is idle",
            "    - the console cable is the wrong way round (it is a rollover,",
            "      not a straight-through)",
            "    - the cable is in an ethernet port rather than the CONSOLE port",
            "",
            "  A silent but healthy device is normal: press Enter in",
            f"  `ciscoyoke console {port}` to wake it, or power-cycle it while",
            f"  `ciscoyoke capture {port}` is listening.",
        ]
    else:
        lines.append(f"  Most plausible: {chosen} baud")
        lines.append(f"  Capture with: ciscoyoke capture {port} --baud {chosen} -o boot.ytx")

    payload: dict[str, object] = {
        "port": port,
        "chosen_baud": chosen,
        "probes": [
            {
                "baud": probe.baud,
                "bytes": probe.bytes_seen,
                "printable": round(probe.printable, 3),
                "plausible": probe.plausible,
            }
            for probe in probes
        ],
    }
    code = ExitCode.SUCCESS if chosen is not None else ExitCode.STATE_UNCERTAIN
    return "\n".join(lines), payload, code


# -- interactive console ----------------------------------------------------


def _reader_thread(
    transport: SerialTransport,
    writer: TranscriptWriter | None,
    started: float,
    stop: threading.Event,
) -> None:
    """Display and record device output continuously.

    A separate thread because the two directions are independent: the device
    talks whenever it likes, and blocking on the keyboard to find that out --
    as the previous version did -- means a boot is invisible until you press
    Enter.
    """
    while not stop.is_set():
        data = transport.read()
        if data:
            _echo(data)
            if writer is not None:
                writer.write_rx(time.monotonic() - started, data)
        else:
            time.sleep(0.01)


def do_console(
    port: str,
    baud: int = DEFAULT_BAUD,
    record: Path | None = None,
) -> int:
    """Interactive console with continuous display and optional recording.

    Line-oriented on input rather than raw-key: a full terminal emulator is a
    project of its own, and PuTTY already exists. What this adds is a session
    captured in the format the rest of the tool replays, so a problem seen by
    hand becomes a fixture.

    ``~break`` on its own line asserts a serial BREAK, which is how a router is
    dropped into ROMMON. It is a command rather than a keystroke because a
    line-oriented reader cannot see Ctrl-Break.
    """
    transport = _open(port, baud)
    started = time.monotonic()
    stop = threading.Event()
    writer = TranscriptWriter(record).open() if record else None

    print(
        f"Connected to {port} at {baud}.\n"
        f"  ~break   assert a serial BREAK (drops a booting router to ROMMON)\n"
        f"  ~quit    disconnect\n"
        f"  Ctrl-C   disconnect\n"
        + (f"Recording to {record}\n" if record else "Not recording.\n"),
        file=sys.stderr,
    )

    reader = threading.Thread(
        target=_reader_thread, args=(transport, writer, started, stop), daemon=True
    )
    reader.start()

    try:
        while True:
            line = sys.stdin.readline()
            if not line:
                break

            command = line.strip().lower()
            if command == "~quit":
                break
            if command == "~break":
                try:
                    transport.send_break()
                    print("[break sent]", file=sys.stderr)
                except (SerialUnavailableError, OSError, AttributeError) as exc:
                    print(f"[break failed: {exc}]", file=sys.stderr)
                continue

            payload = line.rstrip("\n").encode("latin-1") + b"\r"
            transport.write(payload)
            if writer is not None:
                writer.write_tx(time.monotonic() - started, payload)
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        reader.join(timeout=1.0)
        transport.close()
        if writer is not None:
            writer.close()
            print(
                f"\nRaw transcript written to {record} ({writer.count} records).\n"
                f"It is private by default and may contain credentials. "
                f"Scrub it before sharing:\n"
                f"  ciscoyoke transcript scrub {record}",
                file=sys.stderr,
            )

    return int(ExitCode.SUCCESS)


Reporter = Callable[[str], None]
