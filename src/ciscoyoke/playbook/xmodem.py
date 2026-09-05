"""XMODEM, because ROMMON speaks it and nothing else.

A device with no image and no network has exactly one route back: the console
it is already talking on. ROMMON implements XMODEM (and on most platforms
XMODEM-CRC), so that is what gets implemented here -- 128-byte blocks, a
sequence number and its complement, and a checksum or CRC the receiver
acknowledges one block at a time.

The protocol is small. What makes it worth writing carefully is the context:
five megabytes at 9600 baud is around ninety minutes of the receiver
acknowledging blocks, on a device whose only image may have just been deleted.
Three consequences shape the implementation:

* every block is retried rather than abandoned, because one corrupt
  acknowledgement should not cost ninety minutes;
* progress is emitted continuously, because silence is when people pull the
  power;
* cancellation is clean and explicit, because a half-written flash should be
  the result of a decision rather than a timeout.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path

BLOCK_SIZE = 128


class Control(IntEnum):
    """XMODEM control bytes."""

    SOH = 0x01
    """Start of a 128-byte block."""

    EOT = 0x04
    """End of transmission."""

    ACK = 0x06
    """Receiver accepted the block."""

    NAK = 0x15
    """Receiver rejected it; send again. Also the receiver's 'start, checksum
    mode' signal."""

    CAN = 0x18
    """Cancel. Two in a row is the conventional abort."""

    SUB = 0x1A
    """Padding for the final short block."""

    CRC = 0x43
    """ASCII 'C': the receiver's 'start, CRC-16 mode' signal."""


class XmodemError(RuntimeError):
    """The transfer could not be completed."""


class XmodemCancelledError(XmodemError):
    """The receiver cancelled, or the operator did."""


def crc16(data: bytes) -> int:
    """CRC-16/XMODEM over one block.

    Bitwise rather than table-driven: a 128-byte block at 9600 baud takes about
    130 ms to transmit, so the microseconds a lookup table would save are not
    worth the extra code to review.
    """
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc


def checksum(data: bytes) -> int:
    return sum(data) & 0xFF


def blocks(payload: bytes) -> Iterator[tuple[int, bytes]]:
    """Split a payload into numbered 128-byte blocks.

    Block numbers start at 1 and wrap at 255, which is the protocol's own
    behaviour and the reason a transfer cannot be resumed from the middle: the
    sequence number says nothing about absolute position.
    """
    for index in range(0, len(payload), BLOCK_SIZE):
        chunk = payload[index : index + BLOCK_SIZE]
        if len(chunk) < BLOCK_SIZE:
            chunk = chunk + bytes([Control.SUB]) * (BLOCK_SIZE - len(chunk))
        number = (index // BLOCK_SIZE + 1) % 256
        yield number, chunk


def frame(number: int, data: bytes, *, use_crc: bool) -> bytes:
    """Build one block: header, sequence, complement, payload, check."""
    header = bytes([Control.SOH, number, 0xFF - number])
    if use_crc:
        value = crc16(data)
        return header + data + bytes([value >> 8, value & 0xFF])
    return header + data + bytes([checksum(data)])


@dataclass(frozen=True, slots=True)
class TransferOutcome:
    """What happened, in enough detail to explain a failure."""

    sent_bytes: int
    total_bytes: int
    blocks_sent: int
    retries: int
    used_crc: bool
    elapsed: float

    @property
    def complete(self) -> bool:
        return self.sent_bytes >= self.total_bytes

    @property
    def rate(self) -> float:
        return self.sent_bytes / self.elapsed if self.elapsed > 0 else 0.0

    def render(self) -> str:
        mode = "CRC-16" if self.used_crc else "checksum"
        return (
            f"  {self.blocks_sent} blocks, {self.sent_bytes / 1048576:.2f} MiB, "
            f"{self.retries} retries, {mode}, "
            f"{self.elapsed / 60:.1f} min at {self.rate / 1024:.1f} KiB/s"
        )


#: Reads whatever the device has said; may return empty.
Reader = Callable[[], bytes]
#: Writes to the device.
Writer = Callable[[bytes], int]
#: Called with (bytes sent, total bytes) as the transfer progresses.
ProgressHook = Callable[[int, int], None]


def _await_start(
    read: Reader,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    timeout: float,
) -> bool:
    """Wait for the receiver to say it is ready, and in which mode.

    The receiver drives this: it sends ``C`` repeatedly to request CRC mode, or
    ``NAK`` for the older checksum mode. Sending before it asks is the classic
    way to lose the first block and desynchronise everything after it.
    """
    deadline = clock() + timeout
    while clock() < deadline:
        for byte in read():
            if byte == Control.CRC:
                return True
            if byte == Control.NAK:
                return False
            if byte == Control.CAN:
                raise XmodemCancelledError("receiver cancelled before the transfer began")
        sleep(0.05)
    raise XmodemError(
        f"receiver did not request a transfer within {timeout:.0f}s; "
        f"check the device is at a prompt expecting XMODEM"
    )


def send(
    payload: bytes,
    read: Reader,
    write: Writer,
    *,
    progress: ProgressHook | None = None,
    max_retries: int = 10,
    block_timeout: float = 10.0,
    start_timeout: float = 60.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> TransferOutcome:
    """Send ``payload`` by XMODEM, retrying blocks rather than the transfer.

    ``clock`` and ``sleep`` are injected so a ninety-minute transfer can be
    tested in virtual time. Untested progress reporting is how a percentage
    ends up stuck at zero for an hour.
    """
    started = clock()
    use_crc = _await_start(read, clock, sleep, start_timeout)

    sent = 0
    retries = 0
    blocks_sent = 0
    total = len(payload)

    for number, chunk in blocks(payload):
        packet = frame(number, chunk, use_crc=use_crc)
        acknowledged = False

        for _ in range(max_retries):
            write(packet)
            response = _read_response(read, clock, sleep, block_timeout)

            if response == Control.ACK:
                acknowledged = True
                break
            if response == Control.CAN:
                raise XmodemCancelledError(
                    f"receiver cancelled at block {number}; "
                    f"{sent / 1048576:.2f} MiB had been sent"
                )
            # NAK or a timeout: the block is resent. One bad acknowledgement
            # must not cost the whole transfer.
            retries += 1

        if not acknowledged:
            raise XmodemError(
                f"block {number} was not acknowledged after {max_retries} "
                f"attempts; {sent / 1048576:.2f} MiB of "
                f"{total / 1048576:.2f} MiB had been sent"
            )

        blocks_sent += 1
        sent = min(sent + BLOCK_SIZE, total)
        if progress is not None:
            progress(sent, total)

    _finish(write, read, clock, sleep, block_timeout, max_retries)

    return TransferOutcome(
        sent_bytes=sent,
        total_bytes=total,
        blocks_sent=blocks_sent,
        retries=retries,
        used_crc=use_crc,
        elapsed=clock() - started,
    )


def _finish(
    write: Writer,
    read: Reader,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    timeout: float,
    max_retries: int,
) -> None:
    """Close the transfer, and require the receiver to agree that it closed.

    The classic termination is ``EOT`` answered with ``ACK``, but many
    receivers -- including several Cisco bootloaders -- NAK the first ``EOT``
    and acknowledge the second. Both are accepted; silence is not.

    Treating an unanswered ``EOT`` as success was the previous behaviour, and
    it is optimism in the worst possible place: after ninety minutes, on a
    device whose only image may have just been deleted, "probably fine" is not
    a thing to report as a completed transfer.
    """
    for _ in range(max_retries):
        write(bytes([Control.EOT]))
        response = _read_response(read, clock, sleep, timeout)

        if response == Control.ACK:
            return
        if response == Control.CAN:
            raise XmodemCancelledError(
                "receiver cancelled at end of transfer; the image on the "
                "device is incomplete"
            )
        # A NAK here asks for the EOT again, which is a legitimate exchange.
        # Anything else -- including nothing at all -- is retried the same way,
        # because the distinction does not change what we should do next.

    raise XmodemError(
        "the receiver never acknowledged the end of transfer. Every block was "
        "accepted, but the device has not confirmed it wrote the file, so the "
        "image must not be assumed usable. Check the device before booting it."
    )


def _read_response(
    read: Reader,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    timeout: float,
) -> int | None:
    """Wait for a single control byte, or None on timeout."""
    deadline = clock() + timeout
    while clock() < deadline:
        data = read()
        if data:
            # The last byte wins: a receiver that NAKed and then ACKed after a
            # retry has accepted the block, and treating the stale NAK as
            # current would resend a block it already has.
            return data[-1]
        sleep(0.02)
    return None


def send_file(
    path: Path,
    read: Reader,
    write: Writer,
    **kwargs: object,
) -> TransferOutcome:
    """Send a file from disk.

    Read whole rather than streamed: an IOS image for this vintage of hardware
    is a few megabytes, and holding it means a retry never has to seek back
    into a file that might have changed underneath the transfer.
    """
    return send(path.read_bytes(), read, write, **kwargs)  # type: ignore[arg-type]
