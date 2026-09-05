"""Local USB and built-in serial transport.

The first layer that touches real hardware. Everything above it is pure, which
is why everything above it is testable; this module is deliberately thin so the
untestable surface stays small.

Reads are non-blocking-ish by design: :meth:`SerialTransport.read` returns
whatever is currently buffered, including nothing. A console that has said
nothing for a second is a *fact about the device* -- it may be mid-boot, or
asleep, or at the wrong baud -- and the layers above need to see that fact
rather than have it hidden inside a blocking call.
"""

from __future__ import annotations

from types import TracebackType

from ciscoyoke.transport.base import TransportCapabilities
from ciscoyoke.transport.identity import PortIdentity

DEFAULT_BAUD = 9600

# Ordered by likelihood on the hardware this tool targets. 9600 is the Cisco
# console default and the overwhelming majority case; 115200 comes next because
# it is what image-rescue procedures raise the console to, and a device left at
# that speed by an interrupted recovery must be findable again.
CANDIDATE_BAUDS: tuple[int, ...] = (9600, 115200, 38400, 19200, 57600, 4800)

READ_CHUNK = 4096


class SerialUnavailableError(RuntimeError):
    """The port could not be opened."""


class SerialTransport:
    """A console reached over a local serial port.

    Implements the :class:`~ciscoyoke.transport.base.Transport` protocol.
    """

    def __init__(
        self,
        port: str,
        *,
        baud: int = DEFAULT_BAUD,
        identity: PortIdentity | None = None,
        timeout: float = 0.2,
    ) -> None:
        self._port = port
        self._baud = baud
        self._identity = identity
        self._timeout = timeout
        self._serial: object | None = None

    # -- lifecycle ---------------------------------------------------------

    def open(self) -> SerialTransport:
        try:
            import serial
        except ImportError as exc:  # pragma: no cover - hard dependency
            raise SerialUnavailableError("pyserial is not installed") from exc

        try:
            self._serial = serial.Serial(
                port=self._port,
                baudrate=self._baud,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self._timeout,
                write_timeout=2.0,
                # No flow control. A Cisco console does not use it, and leaving
                # RTS/CTS enabled against a device that never asserts CTS is a
                # classic way to produce a port that silently never transmits.
                rtscts=False,
                dsrdtr=False,
                xonxoff=False,
            )
        except Exception as exc:  # serial.SerialException and OS errors
            raise SerialUnavailableError(f"{self._port}: {exc}") from exc
        return self

    def close(self) -> None:
        if self._serial is not None:
            close = getattr(self._serial, "close", None)
            if close is not None:
                close()
            self._serial = None

    def __enter__(self) -> SerialTransport:
        return self.open()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()

    # -- transport surface -------------------------------------------------

    @property
    def name(self) -> str:
        return self._port

    @property
    def capabilities(self) -> TransportCapabilities:
        return TransportCapabilities.local_serial()

    @property
    def baud(self) -> int:
        return self._baud

    def read(self) -> bytes:
        """Return whatever is buffered, possibly nothing."""
        handle = self._require_open()
        waiting = int(getattr(handle, "in_waiting", 0) or 0)
        size = min(waiting, READ_CHUNK) if waiting else 1
        data = handle.read(size)  # type: ignore[attr-defined]
        return bytes(data)

    def write(self, data: bytes) -> int:
        handle = self._require_open()
        written = handle.write(data)  # type: ignore[attr-defined]
        flush = getattr(handle, "flush", None)
        if flush is not None:
            flush()
        return int(written or len(data))

    # -- capability-backed operations --------------------------------------

    def send_break(self, duration: float = 0.25) -> None:
        """Assert a break condition.

        RS-232 defines break as the line held in the space condition; the
        convention is at least 100 ms, and pySerial's default is 0.25 s, which
        is what is used here. Whether a given adapter *actually* drives this is
        exactly what the capability model refuses to assume -- see
        :class:`~ciscoyoke.transport.base.Capability`.
        """
        handle = self._require_open()
        handle.send_break(duration)  # type: ignore[attr-defined]

    def set_baud(self, baud: int) -> None:
        """Change line speed on an open port.

        Used both to raise the console for an image transfer and to probe for a
        device left at a non-default speed by an interrupted recovery.
        """
        handle = self._require_open()
        handle.baudrate = baud  # type: ignore[attr-defined]
        self._baud = baud

    def reset_input(self) -> None:
        """Discard buffered input.

        Called after a baud change, because bytes captured at the previous
        speed are noise at the new one and would otherwise be fed to the state
        tracker as though the device had said them.
        """
        handle = self._require_open()
        reset = getattr(handle, "reset_input_buffer", None)
        if reset is not None:
            reset()

    # -- internals ---------------------------------------------------------

    def _require_open(self) -> object:
        if self._serial is None:
            raise SerialUnavailableError(f"{self._port} is not open")
        return self._serial


def printable_ratio(data: bytes) -> float:
    """Fraction of bytes that look like console text.

    The baud-detection heuristic. IOS output is effectively ASCII, so a correct
    line speed yields a ratio near 1.0 while a wrong one yields high-entropy
    binary noise -- a difference stark enough that no human needs to look at it.
    """
    if not data:
        return 0.0
    good = sum(1 for byte in data if 0x20 <= byte <= 0x7E or byte in (0x09, 0x0A, 0x0D))
    return good / len(data)
