"""XMODEM: framing, retries, cancellation and progress, all in virtual time."""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path

import pytest

from ciscoyoke.playbook.xmodem import (
    BLOCK_SIZE,
    Control,
    XmodemCancelledError,
    XmodemError,
    blocks,
    crc16,
    frame,
    send,
    send_file,
)


class Clock:
    """Virtual time, so a ninety-minute transfer tests in milliseconds."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Receiver:
    """A scripted XMODEM receiver.

    Responds to each block with whatever the script says, which is how retry
    and cancellation paths get exercised without a device.
    """

    def __init__(
        self,
        *,
        use_crc: bool = True,
        responses: list[int] | None = None,
        clock: Clock | None = None,
    ) -> None:
        self.start_byte = Control.CRC if use_crc else Control.NAK
        self.responses = responses or []
        self.received: list[bytes] = []
        self._started = False
        self._clock = clock

    def read(self) -> bytes:
        if not self._started:
            self._started = True
            return bytes([self.start_byte])
        if self.responses:
            return bytes([self.responses.pop(0)])
        return b""

    def write(self, data: bytes) -> int:
        self.received.append(data)
        if self._clock is not None:
            # Blocks take real time on a real line; advancing here means
            # timeouts are exercised rather than bypassed.
            self._clock.now += 0.13
        return len(data)


def acks(count: int) -> list[int]:
    return [Control.ACK] * count


# -- framing ---------------------------------------------------------------


def test_crc16_matches_the_known_vector() -> None:
    """CRC-16/XMODEM of "123456789" is 0x31C3."""
    assert crc16(b"123456789") == 0x31C3


def test_a_block_is_padded_to_128_bytes() -> None:
    numbered = list(blocks(b"short"))

    assert len(numbered) == 1
    number, chunk = numbered[0]
    assert number == 1
    assert len(chunk) == BLOCK_SIZE
    assert chunk.endswith(bytes([Control.SUB]))


def test_block_numbers_start_at_one_and_wrap() -> None:
    numbered = list(blocks(b"\x00" * (BLOCK_SIZE * 256)))

    assert numbered[0][0] == 1
    assert numbered[254][0] == 255
    # 256 wraps to 0, which is the protocol's own behaviour.
    assert numbered[255][0] == 0


def test_a_frame_carries_the_sequence_and_its_complement() -> None:
    packet = frame(7, b"\x00" * BLOCK_SIZE, use_crc=True)

    assert packet[0] == Control.SOH
    assert packet[1] == 7
    assert packet[2] == 0xFF - 7
    assert len(packet) == 3 + BLOCK_SIZE + 2


def test_checksum_mode_frames_are_one_byte_shorter() -> None:
    crc_frame = frame(1, b"\x00" * BLOCK_SIZE, use_crc=True)
    sum_frame = frame(1, b"\x00" * BLOCK_SIZE, use_crc=False)

    assert len(crc_frame) - len(sum_frame) == 1


# -- transfer --------------------------------------------------------------


def test_a_short_payload_transfers() -> None:
    clock = Clock()
    receiver = Receiver(responses=acks(2), clock=clock)

    outcome = send(
        b"hello", receiver.read, receiver.write, clock=clock, sleep=clock.sleep
    )

    assert outcome.complete is True
    assert outcome.blocks_sent == 1
    assert outcome.retries == 0
    assert outcome.used_crc is True
    # One block plus the EOT.
    assert receiver.received[-1] == bytes([Control.EOT])


def test_the_receiver_chooses_checksum_mode() -> None:
    """The receiver drives this; sending before it asks desynchronises."""
    clock = Clock()
    receiver = Receiver(use_crc=False, responses=acks(2), clock=clock)

    outcome = send(
        b"hello", receiver.read, receiver.write, clock=clock, sleep=clock.sleep
    )
    assert outcome.used_crc is False


def test_a_nak_retries_the_block_rather_than_the_transfer() -> None:
    """One corrupt acknowledgement must not cost ninety minutes."""
    clock = Clock()
    receiver = Receiver(
        responses=[Control.NAK, Control.NAK, Control.ACK, Control.ACK], clock=clock
    )

    outcome = send(
        b"hello", receiver.read, receiver.write, clock=clock, sleep=clock.sleep
    )

    assert outcome.complete is True
    assert outcome.retries == 2


def test_persistent_failure_reports_how_far_it_got() -> None:
    """'Timed out' is not diagnosable; 'stopped at 1.9 of 2.9 MiB' is."""
    clock = Clock()
    receiver = Receiver(responses=[Control.NAK] * 40, clock=clock)

    with pytest.raises(XmodemError, match="was not acknowledged"):
        send(
            b"x" * (BLOCK_SIZE * 3),
            receiver.read,
            receiver.write,
            max_retries=3,
            clock=clock,
            sleep=clock.sleep,
        )


def test_receiver_cancellation_stops_immediately() -> None:
    clock = Clock()
    receiver = Receiver(responses=[Control.CAN], clock=clock)

    with pytest.raises(XmodemCancelledError, match="cancelled at block"):
        send(b"hello", receiver.read, receiver.write, clock=clock, sleep=clock.sleep)


def test_cancellation_before_the_start_is_distinguished() -> None:
    clock = Clock()

    class CancellingReceiver:
        def read(self) -> bytes:
            return bytes([Control.CAN])

        def write(self, data: bytes) -> int:
            return len(data)

    receiver = CancellingReceiver()
    with pytest.raises(XmodemCancelledError, match="before the transfer began"):
        send(b"hello", receiver.read, receiver.write, clock=clock, sleep=clock.sleep)


def test_a_receiver_that_never_asks_times_out_with_advice() -> None:
    clock = Clock()

    class SilentReceiver:
        def read(self) -> bytes:
            return b""

        def write(self, data: bytes) -> int:
            return len(data)

    receiver = SilentReceiver()
    with pytest.raises(XmodemError, match="expecting XMODEM"):
        send(
            b"hello",
            receiver.read,
            receiver.write,
            start_timeout=1.0,
            clock=clock,
            sleep=clock.sleep,
        )


def test_progress_is_reported_continuously() -> None:
    """Silence is when people pull the power."""
    clock = Clock()
    payload = b"x" * (BLOCK_SIZE * 5)
    receiver = Receiver(responses=acks(6), clock=clock)

    seen: list[tuple[int, int]] = []
    send(
        payload,
        receiver.read,
        receiver.write,
        progress=lambda sent, total: seen.append((sent, total)),
        clock=clock,
        sleep=clock.sleep,
    )

    assert len(seen) == 5
    assert seen[-1] == (len(payload), len(payload))
    # Monotonic, and never overstating what has been sent.
    assert all(a[0] <= b[0] for a, b in pairwise(seen))
    assert all(sent <= total for sent, total in seen)


def test_a_large_transfer_runs_in_virtual_time() -> None:
    """Five megabytes at 9600 baud is about ninety minutes of real line time."""
    clock = Clock()
    payload = b"x" * (BLOCK_SIZE * 200)
    receiver = Receiver(responses=acks(201), clock=clock)

    outcome = send(
        payload, receiver.read, receiver.write, clock=clock, sleep=clock.sleep
    )

    assert outcome.blocks_sent == 200
    assert outcome.elapsed > 0
    assert "200 blocks" in outcome.render()


def test_send_file_reads_from_disk(tmp_path: Path) -> None:
    path = tmp_path / "ios.bin"
    path.write_bytes(b"\x7fELF" + b"\x00" * 200)

    clock = Clock()
    receiver = Receiver(responses=acks(4), clock=clock)

    outcome = send_file(
        path, receiver.read, receiver.write, clock=clock, sleep=clock.sleep
    )
    assert outcome.complete is True
