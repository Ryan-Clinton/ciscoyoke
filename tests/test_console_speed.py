"""The console speed change around a transfer: ordered, journalled, and proven.

Raising the line to 115200 makes a rescue take minutes rather than hours. It
also means the device and the host can end up at different speeds, which looks
exactly like a dead device. So the order of the change matters, and "restored"
has to be something the device was seen to say, not something assumed.
"""

from __future__ import annotations

from ciscoyoke.image.drivers import CatalystBootloaderDriver
from ciscoyoke.imaging import raise_console_speed, restore_console_speed
from ciscoyoke.playbook.transfer import Method, TransferStep
from ciscoyoke.session import Session
from ciscoyoke.stream.tracker import State
from ciscoyoke.transcript.fake import FakeDevice, FakeTransport
from ciscoyoke.transcript.schema import Direction, Record, Transcript


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


# -- the device is told before the host changes ------------------------------


class RecordingTransport:
    """A transport that records the order of writes and baud changes."""

    def __init__(self) -> None:
        self.events: list[str] = []
        self.baud = 9600

    @property
    def name(self) -> str:
        return "recording"

    @property
    def capabilities(self):  # type: ignore[no-untyped-def]
        from ciscoyoke.transport.base import TransportCapabilities

        return TransportCapabilities.local_serial()

    def read(self) -> bytes:
        return b""

    def write(self, data: bytes) -> int:
        self.events.append(f"tx:{data.decode('latin-1').strip()}")
        return len(data)

    def set_baud(self, baud: int) -> None:
        self.events.append(f"host_baud:{baud}")
        self.baud = baud

    def reset_input(self) -> None:
        self.events.append("reset_input")


def recording_session() -> tuple[Session, RecordingTransport]:
    """A session over a recording transport, in advancing virtual time.

    A clock that never moves turns every deadline loop into an infinite one --
    which is exactly what an earlier version of this test did.
    """
    transport = RecordingTransport()
    clock = Clock()
    session = Session(transport, clock=clock, sleep=clock.sleep)
    return session, transport


def test_the_device_baud_command_precedes_the_host_change() -> None:
    """The bug: the host changed first, so the device saw line noise.

    Cisco's procedure is `set BAUD 115200` and then "the screen goes blank" --
    the device is now talking at a rate the host is not listening at. Doing it
    the other way round leaves a device that appears to have hung.
    """
    session, transport = recording_session()
    step = TransferStep(Method.XMODEM, 1024, "ios.bin", from_baud=9600)

    raise_console_speed(session, step, CatalystBootloaderDriver())

    device_index = transport.events.index("tx:set BAUD 115200")
    host_index = transport.events.index("host_baud:115200")
    assert device_index < host_index


def test_restoring_uses_unset_baud_and_puts_the_host_back() -> None:
    session, transport = recording_session()
    step = TransferStep(Method.XMODEM, 1024, "ios.bin", from_baud=9600)

    restore_console_speed(session, step, CatalystBootloaderDriver())

    assert "tx:unset BAUD" in transport.events
    device_index = transport.events.index("tx:unset BAUD")
    host_index = transport.events.index("host_baud:9600")
    assert device_index < host_index


# -- restoration is observed, not assumed -------------------------------------


class ReplayingSerialTransport(FakeTransport):
    """A replaying transport that also supports baud changes.

    ``FakeTransport`` deliberately implements only the minimum Transport
    protocol, which has no ``set_baud`` -- so restoration bails out early
    against it. Anything exercising the baud path needs a transport that can
    actually change speed, and this is that.
    """

    def __init__(self, device: FakeDevice) -> None:
        super().__init__(device, name="replay+baud")
        self.baud = 9600

    def set_baud(self, baud: int) -> None:
        self.baud = baud

    def reset_input(self) -> None:
        """No-op: discarding here would throw away the replay itself."""
        return None


def baud_capable_session(records: tuple[Record, ...]) -> Session:
    device = FakeDevice(Transcript(records=records), strict=False)
    return Session(
        ReplayingSerialTransport(device),
        clock=device.monotonic,
        sleep=device.sleeper(),
    )


class SilentTransport:
    """A transport that accepts writes and never answers.

    Models the case that mattered: the device did not come back at the restored
    rate.
    """

    def __init__(self) -> None:
        self.baud = 9600

    @property
    def name(self) -> str:
        return "silent"

    @property
    def capabilities(self):  # type: ignore[no-untyped-def]
        from ciscoyoke.transport.base import TransportCapabilities

        return TransportCapabilities.local_serial()

    def read(self) -> bytes:
        return b""

    def write(self, data: bytes) -> int:
        return len(data)

    def set_baud(self, baud: int) -> None:
        self.baud = baud

    def reset_input(self) -> None:
        return None


def test_a_silent_device_is_not_reported_as_restored() -> None:
    """The bug: restoration returned bool(tracker.buffer).

    That buffer accumulates the whole session, so after a flash inventory and
    an XMODEM setup it is never empty -- restoration was reported as verified
    whether or not the device said a word, and the journal then recorded the
    mutation as compensated on no evidence at all.
    """
    clock = Clock()
    session = Session(SilentTransport(), clock=clock, sleep=clock.sleep)
    # Prior traffic, exactly as a real session would have accumulated.
    session.tracker.feed(b"\r\nswitch: \r\nlots of earlier output\r\n")

    step = TransferStep(Method.XMODEM, 1024, "ios.bin", from_baud=9600)
    proof = restore_console_speed(session, step, CatalystBootloaderDriver())

    assert proof.bytes_observed_after_change is False
    assert proof.restored is False
    assert "✗" in proof.render()


def test_bytes_alone_are_not_enough_to_claim_restoration() -> None:
    """A wrong baud produces bytes too -- just not meaningful ones.

    A recognised prompt is what distinguishes a device talking to us from a
    device talking past us.
    """
    session = baud_capable_session(
        (Record(0.0, Direction.RX, b"\xff\xfe\x81\x9a garbage \xc3"),)
    )
    step = TransferStep(Method.XMODEM, 1024, "ios.bin", from_baud=9600)

    proof = restore_console_speed(session, step, CatalystBootloaderDriver())

    assert proof.bytes_observed_after_change is True
    assert proof.prompt_observed is False
    assert proof.restored is False


def test_a_recognised_prompt_after_the_change_is_restoration() -> None:
    session = baud_capable_session((Record(0.0, Direction.RX, b"\r\nswitch: "),))
    step = TransferStep(Method.XMODEM, 1024, "ios.bin", from_baud=9600)

    proof = restore_console_speed(session, step, CatalystBootloaderDriver())

    assert proof.restored is True
    assert proof.state is State.BOOTLOADER
