"""Pure, context-free detectors over console text.

A signal is *evidence*, never a conclusion. ``Password:`` matches the same bytes
whether it is a login prompt, an enable prompt, or a line inside a running-config
someone is paging through; deciding which of those it is requires knowing what
was sent immediately before, and that is the tracker's job (see
:mod:`ciscoyoke.stream.tracker`).

Everything here is a pure function of a string. No I/O, no history, no state --
which is what makes the whole detection path testable without hardware and
property-testable under arbitrary chunking.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class SignalKind(StrEnum):
    """What a detector found. Not a device state."""

    ROMMON_PROMPT = "rommon_prompt"
    BOOTLOADER_PROMPT = "bootloader_prompt"
    SETUP_DIALOG = "setup_dialog"
    PRESS_RETURN = "press_return"
    USERNAME_PROMPT = "username_prompt"
    PASSWORD_PROMPT = "password_prompt"
    USER_EXEC_PROMPT = "user_exec_prompt"
    PRIV_EXEC_PROMPT = "priv_exec_prompt"
    CONFIG_PROMPT = "config_prompt"
    PAGER = "pager"
    RECOVERY_DISABLED = "recovery_disabled"
    BOOT_ACTIVITY = "boot_activity"
    BAD_PASSWORD = "bad_password"
    XMODEM_READY = "xmodem_ready"
    CONFIRM_PROMPT = "confirm_prompt"
    SAVE_CONFIG_PROMPT = "save_config_prompt"
    FILENAME_PROMPT = "filename_prompt"


@dataclass(frozen=True, slots=True)
class Signal:
    """One detector hit, with the text that produced it."""

    kind: SignalKind
    value: str
    context: str

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.kind.value}={self.value!r} ({self.context})"


# Anchored patterns: the device has stopped talking and is waiting for input.
# The trailing ``[ \t]*\Z`` is what distinguishes "the device is sitting at this
# prompt" from "this string happened to scroll past in some output".
_ANCHORED: tuple[tuple[SignalKind, re.Pattern[str], str], ...] = (
    (
        SignalKind.ROMMON_PROMPT,
        re.compile(r"(?:^|[\r\n])(rommon\s+\d+\s*>)[ \t]*\Z"),
        "line_end_after_quiescence",
    ),
    (
        SignalKind.BOOTLOADER_PROMPT,
        re.compile(r"(?:^|[\r\n])(switch:)[ \t]*\Z"),
        "line_end_after_quiescence",
    ),
    (
        SignalKind.SETUP_DIALOG,
        re.compile(
            r"(Would you like to enter the initial configuration dialog\?"
            r"\s*\[yes/no\]:)\s*\Z",
            re.IGNORECASE,
        ),
        "interactive_question",
    ),
    (
        SignalKind.PAGER,
        re.compile(r"(--\s*More\s*--)[ \t]*\Z"),
        "pager_marker",
    ),
    # IOS asks for confirmation constantly, and every destructive command in
    # this project runs into one. `write erase` answers "Erasing the nvram
    # filesystem will remove all files! Continue? [confirm]"; `reload` asks
    # whether to save and then confirms; `delete` asks for the filename and
    # then confirms. Without these as first-class signals a playbook waits for
    # a prompt the device will never reach until the question is answered, and
    # both ends wait until the timeout.
    (
        SignalKind.CONFIRM_PROMPT,
        re.compile(r"(\[confirm\])\s*\Z", re.IGNORECASE),
        "interactive_question",
    ),
    (
        SignalKind.SAVE_CONFIG_PROMPT,
        re.compile(
            r"(System configuration has been modified\.\s*Save\?\s*\[yes/no\]:)\s*\Z",
            re.IGNORECASE,
        ),
        "interactive_question",
    ),
    (
        SignalKind.FILENAME_PROMPT,
        re.compile(
            r"((?:Delete|Source|Destination) filename \[[^\]]*\]\?)\s*\Z",
            re.IGNORECASE,
        ),
        "interactive_question",
    ),
    (
        SignalKind.USERNAME_PROMPT,
        re.compile(r"(?:^|\n)\s*(Username:)\s*\Z"),
        "interactive_question",
    ),
    (
        SignalKind.PASSWORD_PROMPT,
        re.compile(r"(?:^|\n)\s*(Password:)\s*\Z"),
        "interactive_question",
    ),
    (
        SignalKind.CONFIG_PROMPT,
        re.compile(r"(?:^|[\r\n])([\w.\-]+\((?:config)[^)]*\)#)[ \t]*\Z"),
        "line_end_after_quiescence",
    ),
    (
        SignalKind.PRIV_EXEC_PROMPT,
        re.compile(r"(?:^|[\r\n])([\w.\-]+#)[ \t]*\Z"),
        "line_end_after_quiescence",
    ),
    (
        SignalKind.USER_EXEC_PROMPT,
        re.compile(r"(?:^|[\r\n])([\w.\-]+>)[ \t]*\Z"),
        "line_end_after_quiescence",
    ),
    (
        SignalKind.XMODEM_READY,
        re.compile(r"(Begin the Xmodem[^\n]*)\s*\Z", re.IGNORECASE),
        "transfer_ready",
    ),
)

# Unanchored patterns: these are meaningful wherever they appear, because they
# are announcements rather than prompts.
_UNANCHORED: tuple[tuple[SignalKind, re.Pattern[str], str], ...] = (
    (
        SignalKind.RECOVERY_DISABLED,
        re.compile(
            r"(password[- ]recovery (?:mechanism|functionality) "
            r"(?:has been triggered, but is currently disabled|is disabled))",
            re.IGNORECASE,
        ),
        "boot_banner",
    ),
    (
        SignalKind.PRESS_RETURN,
        re.compile(r"(Press RETURN to get started)", re.IGNORECASE),
        "boot_banner",
    ),
    (
        SignalKind.BAD_PASSWORD,
        re.compile(r"(% Bad (?:secrets|passwords)|% Login invalid)", re.IGNORECASE),
        "error_message",
    ),
    (
        SignalKind.BOOT_ACTIVITY,
        re.compile(
            r"(System Bootstrap|Self decompressing the image|"
            r"POST: |Initializing flashfs|Loading \S+\.bin)",
        ),
        "boot_output",
    ),
)


# A console syslog message: optional timestamp, then %FACILITY-SEVERITY-MNEMONIC:
#   *Mar  1 00:12:18.759: %IP_SNMP-3-SOCKET: can't open UDP socket
#   00:00:12: %ENVIRONMENT-2-FAN_FAULT: System Fault: FAN FAULT is detected.
#   18: %LINEPROTO-5-UPDOWN: ...      <- the tail of a timestamp IOS split
# Newlines around it are consumed too, because the damage it does is positional:
# it lands *after* a prompt, so the prompt is no longer at the end of the stream.
_SYSLOG = re.compile(
    r"[\r\n]*"
    r"(?:\*?[A-Z][a-z]{2}\s+\d+\s+)?"
    r"(?:\d+:)*\d+(?:\.\d+)?:\s*"
    r"%[A-Z0-9_]+-\d-[A-Z0-9_]+:[^\r\n]*"
    r"[\r\n]*"
    r"|[\r\n]*%[A-Z0-9_]+-\d-[A-Z0-9_]+:[^\r\n]*[\r\n]*"
)


def strip_syslog(tail: str) -> str:
    """Remove console log messages so they cannot hide a prompt.

    Without ``logging synchronous`` -- and a second-hand switch rarely has it --
    IOS prints log messages over whatever is on the console. A real 2950 did
    this to ``Destination filename [config.text]?``: a link-state message
    arrived after the question, the question stopped being the last thing in
    the stream, and the step waited on a prompt it could no longer see.

    A message in the middle of the stream becomes a single newline, preserving
    line structure for the line-anchored prompts that follow it. One at the end
    is removed entirely, leaving whatever prompt preceded it at the end again.
    """

    def replace(match: re.Match[str]) -> str:
        return "" if match.end() == len(tail) else "\n"

    return _SYSLOG.sub(replace, tail)


def detect(tail: str) -> tuple[Signal, ...]:
    """Return every signal present in ``tail``.

    Pure: the same input always yields the same output. Ordering is stable --
    anchored signals first, in declaration order -- so callers can rely on the
    first anchored hit being the most specific prompt match.
    """
    found: list[Signal] = []

    for kind, pattern, context in _ANCHORED:
        match = pattern.search(tail)
        if match:
            found.append(Signal(kind=kind, value=match.group(1), context=context))

    for kind, pattern, context in _UNANCHORED:
        match = pattern.search(tail)
        if match:
            found.append(Signal(kind=kind, value=match.group(1), context=context))

    return tuple(found)


def first_anchored(signals: tuple[Signal, ...]) -> Signal | None:
    """The most specific prompt signal, if the device is waiting at one.

    ``CONFIG_PROMPT`` is declared before ``PRIV_EXEC_PROMPT`` for a reason:
    ``Router(config)#`` also satisfies the priv-exec pattern, and the more
    specific reading must win.
    """
    anchored_kinds = {kind for kind, _, _ in _ANCHORED}
    for signal in signals:
        if signal.kind in anchored_kinds:
            return signal
    return None
