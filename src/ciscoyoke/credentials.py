"""Credential handling, designed so a secret has nowhere to leak to.

There is deliberately no ``--password`` flag and there never will be. A
credential on a command line lands in shell history, in process listings, and in
any support bundle that captures the invocation -- three channels nobody is
watching, all populated before the tool has done anything at all.

The containment rule, which is asserted by a property test rather than promised
here:

    **No secret supplied through this interface appears in any journal, JSON
    result, support bundle, log event or public transcript.**

``Secret`` exists to make that hard to get wrong by accident. It refuses to
render itself in ``repr`` or ``str``, so a secret caught in an f-string, a
traceback, a log line or a debugger prints its marker rather than its value.
Reaching the plaintext requires calling :meth:`Secret.reveal`, which is greppable
and reviewable -- the point is not that it cannot be done, but that it cannot be
done invisibly.
"""

from __future__ import annotations

import getpass
import sys
from dataclasses import dataclass, field
from typing import Any, Final

REDACTED_DISPLAY: Final = "<secret>"


class Secret:
    """A credential that does not render itself.

    ``__repr__`` and ``__str__`` both return a marker. That covers the accidental
    paths -- f-strings, ``print``, ``logging``, exception messages, ``json.dumps``
    with a default of ``str`` -- which is where credentials actually escape from,
    rather than through deliberate misuse.
    """

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        """The plaintext. Every call site is a deliberate, reviewable decision."""
        return self._value

    def encode(self) -> bytes:
        return self._value.encode("utf-8")

    def __repr__(self) -> str:
        return REDACTED_DISPLAY

    def __str__(self) -> str:
        return REDACTED_DISPLAY

    def __format__(self, spec: str) -> str:
        return REDACTED_DISPLAY

    def __bool__(self) -> bool:
        return bool(self._value)

    def __len__(self) -> int:
        # The length of the marker, not of the secret: leaking length through
        # len() would undo the fixed-width redaction the recorder is careful to
        # apply.
        return len(REDACTED_DISPLAY)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Secret):
            return self._value == other._value
        return NotImplemented

    def __hash__(self) -> int:
        return hash(self._value)


@dataclass(frozen=True, slots=True)
class Credentials:
    """What is needed to reach a privileged prompt."""

    username: str | None = None
    password: Secret | None = field(default=None, repr=False)
    enable_password: Secret | None = field(default=None, repr=False)

    @property
    def empty(self) -> bool:
        return not (self.username or self.password or self.enable_password)

    def to_json(self) -> dict[str, Any]:
        """Serialisation that cannot carry a secret.

        Only whether each credential was supplied, never its value. This is the
        method a ``--json`` result or a support bundle would reach for, so it is
        the one that must be incapable of leaking.
        """
        return {
            "username": self.username,
            "password": bool(self.password),
            "enable_password": bool(self.enable_password),
        }


class CredentialsUnavailableError(RuntimeError):
    """Credentials were needed but could not be obtained."""


def prompt(
    *,
    need_username: bool = True,
    need_enable: bool = True,
    stream: Any = None,
) -> Credentials:
    """Ask the operator interactively.

    ``getpass`` reads without echoing and, importantly, without the value ever
    becoming a shell argument.
    """
    target = stream if stream is not None else sys.stderr

    username = None
    if need_username:
        print("Username: ", end="", file=target, flush=True)
        username = sys.stdin.readline().strip() or None

    password = Secret(getpass.getpass("Password: ", stream=target))
    enable = Secret(getpass.getpass("Enable password: ", stream=target)) if need_enable else None

    return Credentials(
        username=username,
        password=password if password else None,
        enable_password=enable if enable else None,
    )


def from_stdin() -> Credentials:
    """Read a password from stdin, for automation.

    The supported non-interactive path: ``--password-stdin``. A pipe is not a
    command line -- it does not reach the process table or the shell history --
    which is the whole reason this exists and a flag does not.
    """
    data = sys.stdin.read()
    if not data:
        raise CredentialsUnavailableError("no credential supplied on stdin")
    lines = data.splitlines()
    password = Secret(lines[0].strip())
    enable = Secret(lines[1].strip()) if len(lines) > 1 and lines[1].strip() else None
    return Credentials(password=password, enable_password=enable)


def scrub_value(value: object) -> object:
    """Replace any :class:`Secret` with its marker, recursively.

    A last line of defence for structures on their way into a journal, a result
    or a support bundle. The containment property test asserts the sentinel
    never appears in any of those; this is what makes that true even when a
    structure is assembled somewhere that did not think about it.
    """
    if isinstance(value, Secret):
        return REDACTED_DISPLAY
    if isinstance(value, dict):
        return {key: scrub_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(scrub_value(item) for item in value)
    return value
