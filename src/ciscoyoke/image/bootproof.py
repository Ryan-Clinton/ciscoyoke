"""Proving a rescued device actually came back.

A completed transfer establishes that the bytes arrived. It says nothing about
whether the device can boot them, and on a box whose only image was replaced
that is the entire question.

The specification named boot proof as the definitive success criterion, and it
means four things, checked in order:

1. the image was loaded;
2. IOS reached a prompt;
3. ``show version`` reports the image and version expected;
4. every temporary change -- console speed above all -- has been put back.

Anything short of all four is reported as what it is. "Transferred, unverified"
is a legitimate and useful outcome; calling it a rescue is not.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from ciscoyoke.identify import facts as facts_module
from ciscoyoke.image.fingerprint import Fingerprint
from ciscoyoke.session import Session, SessionTimeoutError
from ciscoyoke.stream.tracker import State

#: States that mean IOS is up and talking.
_BOOTED = (State.USER_EXEC, State.PRIV_EXEC, State.PRESS_RETURN, State.SETUP_DIALOG)


class ProofOutcome(StrEnum):
    PASS = "pass"
    """Loaded, booted, version confirmed, console restored."""

    BOOTED_UNVERIFIED = "booted_unverified"
    """IOS came up but the running version could not be confirmed."""

    DID_NOT_BOOT = "did_not_boot"
    """The device never reached IOS."""

    NOT_ATTEMPTED = "not_attempted"
    """Nothing was booted, so nothing is proven."""


@dataclass(frozen=True, slots=True)
class BootProof:
    """What was actually established after a rescue."""

    outcome: ProofOutcome
    checks: tuple[tuple[str, bool | None, str], ...] = field(default=())
    running_version: str | None = None
    running_image: str | None = None

    @property
    def proven(self) -> bool:
        return self.outcome is ProofOutcome.PASS

    def render(self) -> str:
        marks = {True: "✓", False: "✗", None: "?"}
        lines = ["BOOT PROOF", ""]
        lines.extend(
            f"  {marks[passed]} {name:<34} {detail}"
            for name, passed, detail in self.checks
        )
        lines += ["", f"  Result: {self.outcome.value}"]
        if self.outcome is not ProofOutcome.PASS:
            lines.append(
                "  A completed transfer proves the bytes arrived, not that they "
                "boot. Treat this device as unverified."
            )
        return "\n".join(lines)

    def to_json(self) -> dict[str, object]:
        return {
            "outcome": self.outcome.value,
            "proven": self.proven,
            "running_version": self.running_version,
            "running_image": self.running_image,
            "checks": [
                {"check": name, "passed": passed, "detail": detail}
                for name, passed, detail in self.checks
            ],
        }


def prove(
    session: Session,
    image: Fingerprint,
    *,
    boot_command: bytes = b"boot\r",
    boot_timeout: float = 300.0,
    console_restored: bool = False,
) -> BootProof:
    """Boot the device and establish what came back.

    Waits for IOS rather than sleeping: a cold boot on this vintage of hardware
    is twenty to forty seconds and a reload can be minutes, so a fixed wait is
    either too short to be reliable or too long to be bearable.
    """
    checks: list[tuple[str, bool | None, str]] = []

    session.send(boot_command)
    try:
        session.wait_for(_BOOTED, timeout=boot_timeout)
    except SessionTimeoutError:
        checks.append(
            (
                "image loaded and IOS reached",
                False,
                f"device is at {session.state.state.value} after "
                f"{int(boot_timeout)}s",
            )
        )
        return BootProof(ProofOutcome.DID_NOT_BOOT, tuple(checks))

    checks.append(("image loaded and IOS reached", True, session.state.state.value))

    if session.state.state in (State.PRESS_RETURN, State.SETUP_DIALOG):
        # A device with no configuration offers the setup dialog; declining it
        # is how the prompt is reached, and it changes nothing.
        session.send(b"no\r\r")
        session.settle(timeout=30.0)

    running = _read_version(session)
    if running is None:
        checks.append(
            ("running version confirmed", None, "show version could not be read")
        )
        return BootProof(ProofOutcome.BOOTED_UNVERIFIED, tuple(checks))

    version, image_file = running
    checks.append(("running version confirmed", True, version or "unknown"))

    # The identity check that matters. Comparing versions alone would pass a
    # device running c2950-*otherfeatures*-mz.121-22.EA14.bin after being sent
    # c2950-i6q4l2-mz.121-22.EA14.bin: same IOS version, different image, and
    # the feature set is the whole reason someone chose one over the other.
    running_name = _basename(image_file)
    expected_name = image.name
    image_matched: bool | None
    if running_name is None:
        image_matched = None
        image_detail = "show version did not report a system image file"
    else:
        image_matched = running_name.lower() == expected_name.lower()
        image_detail = f"expected {expected_name}, running {running_name}"
    checks.append(("running image is the supplied image", image_matched, image_detail))

    expected = image.version
    matched = _versions_agree(expected, version)
    checks.append(
        (
            "version matches the supplied image",
            matched,
            f"expected {expected or 'unknown'}, running {version or 'unknown'}",
        )
    )

    checks.append(
        (
            "console speed restored",
            console_restored,
            "restored to the original rate" if console_restored else "not restored",
        )
    )

    if image_matched and matched is not False and console_restored:
        return BootProof(
            ProofOutcome.PASS,
            tuple(checks),
            running_version=version,
            running_image=image_file,
        )
    return BootProof(
        ProofOutcome.BOOTED_UNVERIFIED,
        tuple(checks),
        running_version=version,
        running_image=image_file,
    )


def _read_version(session: Session) -> tuple[str | None, str | None] | None:
    before = len(session.tracker.buffer)
    session.send(b"terminal length 0\r")
    session.settle()
    session.send(b"show version\r")
    session.settle(timeout=20.0)
    session.drain_pager()

    output = session.tracker.buffer.text[before:]
    if "show version" not in output.lower():
        return None

    parsed = facts_module.from_show_version(output)
    image_file = _running_image(output)
    return parsed.ios_version.value, image_file


def _running_image(output: str) -> str | None:
    for line in output.splitlines():
        if "System image file is" in line:
            _, _, tail = line.partition("is")
            return tail.strip().strip('"')
    return None


def _basename(path: str | None) -> str | None:
    """The filename from ``flash:c2950-...bin`` or ``flash:/dir/c2950-...bin``.

    IOS reports the image with a device prefix and sometimes a directory; the
    supplied image is a local filename. Comparing the basenames is the only
    part that is meaningfully the same thing.
    """
    if not path:
        return None
    tail = path.rsplit(":", 1)[-1]
    return tail.rsplit("/", 1)[-1].strip() or None


def _versions_agree(expected: str | None, running: str | None) -> bool | None:
    """Compare a filename-derived version against what IOS reports.

    Returns None rather than False when the comparison cannot be made. The
    filename convention encodes ``121-22.EA14`` where IOS reports
    ``12.1(22)EA14``, so the two are normalised to digits before comparing --
    and an image whose name never carried a version cannot disagree with
    anything.
    """
    if not expected or not running:
        return None
    return _digits(expected) == _digits(running)


def _digits(text: str) -> str:
    return "".join(character for character in text if character.isalnum()).lower()
