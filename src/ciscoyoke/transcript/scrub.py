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
        # Anything between the keyword and `key`: a real 2950 had
        # "radius-server host H auth-port 1812 acct-port 1813 key K", and an
        # earlier pattern allowing only `host H` left a live shared key in the
        # scrubbed output.
        re.compile(
            r"((?:tacacs-server|radius-server)\b[^\r\n]*?\bkey\s+(?:\d+\s+)?)(\S+)",
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

# A banner is free text chosen by the previous owner -- a real one gave the
# organisation's name and street address -- so its body goes, whatever it says.
# `banner exec ^C ... ^C`: the delimiter is the first character after the type.
_BANNER = re.compile(
    r"(banner\s+(?:motd|exec|login|incoming|prompt-timeout|slip-ppp)\s+)"
    r"(\^C|\S)(.*?)(\2)",
    re.IGNORECASE | re.DOTALL,
)

_HOSTNAME = re.compile(r"(hostname\s+)(\S+)", re.IGNORECASE)

# Places a device names itself other than `hostname X`: the console-available
# log line and its own prompts. A hostname learned anywhere is then replaced
# everywhere, because a real 2950's hostname was a site reference number that
# appeared in every prompt, not just in the configuration.
_HOSTNAME_SIGHTINGS = (
    re.compile(r"(?:^|[\r\n])([A-Za-z0-9][\w.\-]*) con0 is now available"),
    re.compile(r"(?:^|[\r\n])([A-Za-z0-9][\w.\-]*)(?:\(config[^)]*\))?[#>]"),
)
# Names every device shows with no configuration; not identifying.
_GENERIC_HOSTNAMES = frozenset({"Switch", "Router", "switch", "device"})

# Unit identity: enough to trace a device back to an asset register.
_SERIAL = re.compile(
    r"((?:System serial number|Motherboard serial number|Power supply serial number|"
    r"Processor board ID)\s*:?\s*|(?:SYSTEM|MOTHERBOARD|POWER_SUPPLY)_SERIAL_NUM=)"
    r"([A-Z0-9]{6,})",
    re.IGNORECASE,
)
_MAC = re.compile(
    r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b|\b(?:[0-9a-f]{4}\.){2}[0-9a-f]{4}\b"
)


@dataclass(frozen=True, slots=True)
class ScrubResult:
    """What a scrub pass changed, and what it cannot promise."""

    transcript: Transcript
    credentials: int = 0
    addresses: int = 0
    hostnames: int = 0
    key_blocks: int = 0
    identifiers: int = 0

    @property
    def total(self) -> int:
        return (
            self.credentials
            + self.addresses
            + self.hostnames
            + self.key_blocks
            + self.identifiers
        )

    def report(self) -> str:
        """Human-readable summary, always including the caveat."""
        lines = [
            "SCRUB RESULT",
            "",
            f"  {self.credentials:3d} credential-like values redacted",
            f"  {self.addresses:3d} IPv4 addresses anonymised",
            f"  {self.hostnames:3d} hostnames anonymised",
            f"  {self.key_blocks:3d} key blocks removed",
            f"  {self.identifiers:3d} serial numbers and MAC addresses anonymised",
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
        self.identifiers = 0


def _scrub_text(text: str, counter: _Counter, addresses: dict[str, str]) -> str:
    def redact_credential(match: re.Match[str]) -> str:
        counter.credentials += 1
        return f"{match.group(1)}{_REDACTION}"

    def drop_banner(match: re.Match[str]) -> str:
        counter.credentials += 1
        return f"{match.group(1)}{match.group(2)}\n<banner removed>\n{match.group(4)}"

    # Before credentials: a banner that mentions "password" is prose, and
    # redacting a word out of it would leave the rest of the prose behind.
    text = _BANNER.sub(drop_banner, text)

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

    text = _HOSTNAME.sub(anonymise_hostname, text)

    def anonymise_serial(match: re.Match[str]) -> str:
        counter.identifiers += 1
        return f"{match.group(1)}SERIAL0000"

    text = _SERIAL.sub(anonymise_serial, text)

    def anonymise_mac(match: re.Match[str]) -> str:
        # RFC 7042 documentation MAC, in the notation the device used.
        counter.identifiers += 1
        return "0000.5e00.5301" if "." in match.group(0) else "00:00:5e:00:53:01"

    return _MAC.sub(anonymise_mac, text)


def _coalesce(records: tuple[Record, ...]) -> list[Record]:
    """Join consecutive records in the same direction into one.

    A real console delivers a line a few bytes per read -- a 2950 split
    ``Username`` across two -- so a secret can straddle records, and a pattern
    applied record by record never sees it whole. Merging each run of same-
    direction bytes (keeping the first timestamp) puts every line back together
    before anything is matched. Secret-flagged records are never merged: they
    are already redacted and must stay distinguishable.
    """
    merged: list[Record] = []
    for record in records:
        previous = merged[-1] if merged else None
        if (
            previous is not None
            and not previous.secret
            and not record.secret
            and previous.direction is record.direction
        ):
            merged[-1] = Record(
                t=previous.t,
                direction=previous.direction,
                data=previous.data + record.data,
                secret=False,
            )
        else:
            merged.append(record)
    return merged


def _hostnames(records: list[Record]) -> tuple[str, ...]:
    """Every non-generic name the device called itself, longest first."""
    text = "".join(r.data.decode("latin-1") for r in records if not r.secret)
    found: set[str] = {m.group(2) for m in _HOSTNAME.finditer(text)}
    for pattern in _HOSTNAME_SIGHTINGS:
        found.update(m.group(1) for m in pattern.finditer(text))
    return tuple(sorted(found - _GENERIC_HOSTNAMES, key=lambda n: (-len(n), n)))


def scrub(transcript: Transcript, *, source: str | None = None) -> ScrubResult:
    """Produce a shareable transcript from a raw one.

    Deterministic: address placeholders are assigned in first-appearance order,
    so the same input always yields the same output.
    """
    counter = _Counter()
    addresses: dict[str, str] = {}

    records = _coalesce(tuple(transcript))
    names = _hostnames(records)

    scrubbed: list[Record] = []
    for record in records:
        if record.secret:
            scrubbed.append(record)
            continue
        text = record.data.decode("latin-1")
        for name in names:
            occurrences = text.count(name)
            if occurrences:
                counter.hostnames += occurrences
                text = text.replace(name, "device")
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
        identifiers=counter.identifiers,
    )
