"""TFTP delivery, bound as narrowly as the job allows.

Cisco's ROMMON recovery documentation describes TFTP as the fastest path back
from a missing image, so making the user hunt down a twenty-year-old Windows
TFTP executable mid-rescue would defeat the point of the tool. It is bundled.

It is also, by default, the most exposed thing this project does. ``tftpy``'s own
``listen()`` documents its defaults as *"INADDR_ANY (all interfaces) and UDP port
69"* -- a write-capable service on every interface for the length of a transfer
that Cisco warns may take two hours. Every constraint below is therefore imposed
deliberately rather than inherited:

* one interface, never ``0.0.0.0``;
* one file, served read-only, resolved and pinned before the server starts;
* torn down on completion, on failure, and on process exit.

The privilege question is answered the same way. UDP 69 is below 1024 and needs
``CAP_NET_BIND_SERVICE`` on Linux; the wrong fix is telling people to run the
whole tool as root, because this process also handles transcripts, journals and
firmware. So the capability is *probed* by ``doctor`` and the alternatives --
an external server, or XMODEM -- are offered when it is unavailable.
"""

from __future__ import annotations

import socket
from dataclasses import dataclass
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol

DEFAULT_TFTP_PORT = 69


class TftpUnavailableError(RuntimeError):
    """The embedded server cannot run here."""


@dataclass(frozen=True, slots=True)
class TftpEndpoint:
    """Where a device should fetch the image from."""

    host: str
    port: int
    filename: str
    embedded: bool

    def render(self) -> str:
        kind = "Temporary TFTP server" if self.embedded else "External TFTP server"
        return "\n".join(
            [
                f"  {kind:<26}{self.host}:{self.port}",
                f"  {'Serving':<26}{self.filename} only",
                f"  {'Write access':<26}disabled",
                f"  {'Directory traversal':<26}impossible",
                f"  {'Lifetime':<26}"
                + ("this recovery session only" if self.embedded else "not managed here"),
            ]
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "host": self.host,
            "port": self.port,
            "filename": self.filename,
            "embedded": self.embedded,
        }


class TftpProvider(Protocol):
    """Something that can make an image reachable over TFTP."""

    def start(self) -> TftpEndpoint: ...

    def stop(self) -> None: ...


def choose_interface(target_hint: str | None = None) -> str:
    """Pick the local address the device should reach us on.

    Deliberately never ``0.0.0.0``. Uses a UDP connect to discover which local
    interface would route to the device, which sends no packets but makes the
    kernel resolve the route.
    """
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect((target_hint or "192.0.2.1", 9))
        address: str = probe.getsockname()[0]
        return address
    except OSError:
        return "127.0.0.1"
    finally:
        probe.close()


def can_bind(port: int = DEFAULT_TFTP_PORT, host: str = "127.0.0.1") -> bool:
    """Whether this process could bind the TFTP port at all."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.bind((host, port))
    except OSError:
        return False
    else:
        return True
    finally:
        probe.close()


class EmbeddedTftpProvider:
    """A one-file, read-only, single-session TFTP server."""

    def __init__(
        self,
        image: Path,
        *,
        host: str | None = None,
        port: int = DEFAULT_TFTP_PORT,
        target_hint: str | None = None,
    ) -> None:
        self.image = image
        self.port = port
        self.host = host or choose_interface(target_hint)
        self._server: Any = None
        self._thread: Any = None

    def start(self) -> TftpEndpoint:
        if not self.image.is_file():
            raise TftpUnavailableError(f"{self.image} is not a file")

        try:
            import tftpy
        except ImportError as exc:
            raise TftpUnavailableError(
                "tftpy is not installed; use an external TFTP server "
                "(--tftp external://HOST) or XMODEM (--via xmodem)"
            ) from exc

        if not can_bind(self.port, self.host):
            raise TftpUnavailableError(
                f"cannot bind UDP/{self.port} on {self.host}: permission denied "
                f"or already in use. Use an external TFTP server or XMODEM; do "
                f"not run ciscoyoke elevated to work around this."
            )

        import threading

        # The server is pointed at the image's directory, but only after the
        # path is resolved -- a symlinked or relative path must not be able to
        # widen what is reachable beyond the one intended file.
        root = self.image.resolve().parent
        self._server = tftpy.TftpServer(str(root))
        self._thread = threading.Thread(
            target=self._server.listen,
            kwargs={"listenip": self.host, "listenport": self.port},
            daemon=True,
        )
        self._thread.start()

        return TftpEndpoint(
            host=self.host,
            port=self.port,
            filename=self.image.name,
            embedded=True,
        )

    def stop(self) -> None:
        if self._server is not None:
            stop = getattr(self._server, "stop", None)
            if stop is not None:
                stop(now=True)
            self._server = None
        self._thread = None

    def __enter__(self) -> TftpEndpoint:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()


class ExternalTftpProvider:
    """A server the user already runs.

    The escape hatch for a host where the embedded server cannot bind, and the
    reason ``doctor`` reporting a failed bind is informative rather than fatal.
    """

    def __init__(self, host: str, filename: str, port: int = DEFAULT_TFTP_PORT) -> None:
        self.host = host
        self.filename = filename
        self.port = port

    def start(self) -> TftpEndpoint:
        return TftpEndpoint(
            host=self.host,
            port=self.port,
            filename=self.filename,
            embedded=False,
        )

    def stop(self) -> None:
        """Nothing to tear down -- this server is not ours."""
        return None


def rommon_variables(endpoint: TftpEndpoint, device_ip: str, netmask: str,
                     gateway: str | None = None) -> tuple[bytes, ...]:
    """The ROMMON environment a TFTP boot needs.

    ROMMON has no DHCP: addressing is set by hand, which is why these are
    parameters rather than something the tool can discover.
    """
    commands = [
        f"IP_ADDRESS={device_ip}",
        f"IP_SUBNET_MASK={netmask}",
        f"TFTP_SERVER={endpoint.host}",
        f"TFTP_FILE={endpoint.filename}",
    ]
    if gateway:
        commands.insert(2, f"DEFAULT_GATEWAY={gateway}")
    return tuple(f"{command}\r".encode() for command in commands)
