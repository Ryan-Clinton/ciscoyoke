"""Deterministic raw -> shareable transcript transformation.

This is the *second* line of defence. The first is the recorder, which never
writes a credential it was told about (see :mod:`ciscoyoke.transcript.recorder`).
Scrubbing exists for the far larger problem: secrets that arrive in RX, in
configuration the device volunteered, that nothing marked as sensitive.

Two properties matter and both are tested:

* **Deterministic.** Identical input yields byte-identical output, so a fixture
  regenerated later does not produce a spurious diff.
* **Honest.** :class:`ScrubResult` reports what was found and states plainly
  that absence of findings is not proof of cleanliness. Automated scrubbing
  cannot prove a transcript contains no sensitive data, and claiming otherwise
  would be worse than not scrubbing at all, because it would stop people
  looking.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ciscoyoke.transcript.schema import Record, Transcript

SCRUB_VERSION = 1

_REDACTION = "<redacted>"

# Ordered: the first pattern to match a span wins, so specific credential forms
# are consumed before the generic address/hostname sweeps see them.
_CREDENTIAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "enable_secret",
        re.compile(r"(enable\s+(?:secret|password)\s+(?:\d+\s+)?)(\S+)", re.IGNORECASE),
    ),
    (
        "username_secret",
        re.compile(
            r"(username\s+\S+\s+(?:privilege\s+\d+\s+)?"
            r"(?:secret|password)\s+(?:\d+\s+)?)(\S+)",
            re.IGNORECASE,
        ),
    ),
    (
        "snmp_community",
        re.compile(r"(snmp-server\s+community\s+)(\S+)", re.IGNORECASE),
    ),
    (
        "shared_key",
        re.compile(
            r"((?:tacacs-server|radius-server)\s+(?:host\s+\S+\s+)?key\s+"
            r"(?:\d+\s+)?)(\S+)",
            re.IGNORECASE,
        ),
    ),
    (
        "line_password",
        re.compile(r"(password\s+(?:\d+\s+)?)(\S+)", re.IGNORECASE),
    ),
    (
        "pre_shared_key",
        re.compile(r"((?:pre-shared-key|key-string)\s+)(\S+)", re.IGNORECASE),
    ),
)

_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
    re.DOTALL,
)

# Cisco's own key export blocks are not PEM-delimited; they are a run of hex.
_CISCO_KEY_BLOCK = re.compile(
    r"(?:^|\n)(?:[0-9A-F]{8}\s+){6,}[0-9A-F]{0,8}",
)

_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")

_HOSTNAME = re.compile(r"(hostname\s+)(\S+)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class ScrubResult:
    """What a scrub pass changed, and what it cannot promise."""

    transcript: Transcript
    credentials: int = 0
    addresses: int = 0
    hostnames: int = 0
    key_blocks: int = 0

    @property
    def total(self) -> int:
        return self.credentials + self.addresses + self.hostnames + self.key_blocks

    def report(self) -> str:
        """Human-readable summary, always including the caveat."""
        lines = [
            "SCRUB RESULT",
            "",
            f"  {self.credentials:3d} credential-like values redacted",
            f"  {self.addresses:3d} IPv4 addresses anonymised",
            f"  {self.hostnames:3d} hostnames anonymised",
            f"  {self.key_blocks:3d} key blocks removed",
            "",
            "WARNING: automated scrubbing cannot prove a transcript contains no",
            "sensitive data. Review before publishing.",
        ]
        return "\n".join(lines)


class _Counter:
    def __init__(self) -> None:
        self.credentials = 0
        self.addresses = 0
        self.hostnames = 0
        self.key_blocks = 0


def _scrub_text(text: str, counter: _Counter, addresses: dict[str, str]) -> str:
    def redact_credential(match: re.Match[str]) -> str:
        counter.credentials += 1
        return f"{match.group(1)}{_REDACTION}"

    for _name, pattern in _CREDENTIAL_PATTERNS:
        text = pattern.sub(redact_credential, text)

    def drop_key(match: re.Match[str]) -> str:
        counter.key_blocks += 1
        return "<key block removed>"

    text = _PRIVATE_KEY_BLOCK.sub(drop_key, text)
    text = _CISCO_KEY_BLOCK.sub(lambda m: "\n" + drop_key(m), text)

    def anonymise_address(match: re.Match[str]) -> str:
        original = match.group(0)
        # Stable mapping within a transcript: the same address always becomes
        # the same placeholder, so topology remains legible without disclosing
        # the real addressing.
        if original not in addresses:
            addresses[original] = f"10.0.0.{len(addresses) + 1}"
            counter.addresses += 1
        return addresses[original]

    text = _IPV4.sub(anonymise_address, text)

    def anonymise_hostname(match: re.Match[str]) -> str:
        counter.hostnames += 1
        return f"{match.group(1)}device"

    return _HOSTNAME.sub(anonymise_hostname, text)


def scrub(transcript: Transcript, *, source: str | None = None) -> ScrubResult:
    """Produce a shareable transcript from a raw one.

    Deterministic: address placeholders are assigned in first-appearance order,
    so the same input always yields the same output.
    """
    counter = _Counter()
    addresses: dict[str, str] = {}

    scrubbed: list[Record] = []
    for record in transcript:
        if record.secret:
            scrubbed.append(record)
            continue
        text = record.data.decode("latin-1")
        cleaned = _scrub_text(text, counter, addresses)
        scrubbed.append(
            Record(
                t=record.t,
                direction=record.direction,
                data=cleaned.encode("latin-1"),
                secret=record.secret,
            )
        )

    return ScrubResult(
        transcript=Transcript(
            records=tuple(scrubbed),
            public=True,
            scrub_version=SCRUB_VERSION,
            source=source,
        ),
        credentials=counter.credentials,
        addresses=counter.addresses,
        hostnames=counter.hostnames,
        key_blocks=counter.key_blocks,
    )
