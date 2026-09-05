"""Remote console endpoints: ser2net, ConsolePi, and bare sockets.

Someone who already owns a console server should get into the same state engine
without ciscoyoke trying to become one. pySerial's URL handlers make that nearly
free to implement -- but "nearly free to implement" is not the same as free to
use, and the capability model is where the difference gets stated rather than
discovered.

pySerial's own documentation is blunt about both handlers: for ``socket://``,
*"All serial port settings, control and status lines are ignored. Only data is
transmitted and received"*, and for both, *"The connection is not encrypted and
no authentication is supported! Only use it in trusted environments."*

So a bare socket cannot send a break or change baud, which rules out both routes
into ROMMON. A playbook needing either fails at plan time on that transport --
before a device has been power-cycled in expectation of a break that can never
arrive.
"""

from __future__ import annotations

from types import TracebackType

from ciscoyoke.transport.base import TransportCapabilities

READ_CHUNK = 4096


class RemoteUnavailableError(RuntimeError):
    """The remote endpoint could not be opened."""


class RemoteTransport:
    """A console reached over ``rfc2217://`` or ``socket://``.

    Both are plaintext. The capability set differs sharply between them, which
    is the whole reason they are distinguished rather than treated as one
    "remote" transport.
    """

    def __init__(self, url: str, *, baud: int = 9600, timeout: float = 0.5) -> None:
        if not url.startswith(("rfc2217://", "socket://")):
            raise RemoteUnavailableError(
                f"{url!r}: expected an rfc2217:// or socket:// URL"
            )
        self._url = url
        self._baud = baud
        self._timeout = timeout
        self._serial: object | None = None

    @property
    def name(self) -> str:
        return self._url

    @property
    def controllable(self) -> bool:
        """Whether this endpoint carries serial control as well as data."""
        return self._url.startswith("rfc2217://")

    @property
    def capabilities(self) -> TransportCapabilities:
        if self.controllable:
            return TransportCapabilities.rfc2217()
        return TransportCapabilities.raw_socket()

    @property
    def encrypted(self) -> bool:
        """Always false. Stated as a property so callers can warn on it."""
        return False

    def open(self) -> RemoteTransport:
        try:
            import serial
        except ImportError as exc:  # pragma: no cover - hard dependency
            raise RemoteUnavailableError("pyserial is not installed") from exc

        try:
            # Longer default timeouts than a local port: a network round-trip
            # sits inside every read of an already timing-sensitive protocol.
            self._serial = serial.serial_for_url(
                self._url, baudrate=self._baud, timeout=self._timeout
            )
        except Exception as exc:
            raise RemoteUnavailableError(f"{self._url}: {exc}") from exc
        return self

    def close(self) -> None:
        if self._serial is not None:
            close = getattr(self._serial, "close", None)
            if close is not None:
                close()
            self._serial = None

    def read(self) -> bytes:
        handle = self._require_open()
        waiting = int(getattr(handle, "in_waiting", 0) or 0)
        size = min(waiting, READ_CHUNK) if waiting else 1
        return bytes(handle.read(size))  # type: ignore[attr-defined]

    def write(self, data: bytes) -> int:
        handle = self._require_open()
        written = handle.write(data)  # type: ignore[attr-defined]
        return int(written or len(data))

    def send_break(self, duration: float = 0.25) -> None:
        """Assert a break, where the protocol carries one.

        RFC2217 defines a break command; a bare socket has nowhere to put it.
        Refusing loudly here is better than silently doing nothing, which would
        look like a device that ignored the break.
        """
        if not self.controllable:
            raise RemoteUnavailableError(
                "socket:// carries data only; it cannot send a break. Use "
                "rfc2217:// or a local serial port."
            )
        handle = self._require_open()
        handle.send_break(duration)  # type: ignore[attr-defined]

    def set_baud(self, baud: int) -> None:
        if not self.controllable:
            raise RemoteUnavailableError(
                "socket:// carries data only; it cannot change line speed."
            )
        handle = self._require_open()
        handle.baudrate = baud  # type: ignore[attr-defined]
        self._baud = baud

    def _require_open(self) -> object:
        if self._serial is None:
            raise RemoteUnavailableError(f"{self._url} is not open")
        return self._serial

    def __enter__(self) -> RemoteTransport:
        return self.open()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def security_warning(url: str) -> str:
    """The warning a caller should print before sending credentials."""
    return (
        f"{url} is not encrypted and not authenticated. Anything sent over it, "
        f"including passwords, is visible on the network. Use it only on a "
        f"network you trust."
    )
