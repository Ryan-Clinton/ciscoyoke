"""Scrubbing: deterministic, RX-aware, and honest about what it cannot prove."""

from __future__ import annotations

from ciscoyoke.transcript.schema import Direction, Record, Transcript
from ciscoyoke.transcript.scrub import scrub

RUNNING_CONFIG = b"""Router#show running-config
Building configuration...

hostname CORE-SW-01
!
enable secret 5 $1$mERr$3vFkYt7dQ2pLxWq8nZ1
!
username admin privilege 15 secret 5 $1$abcd$efghijklmnop
!
snmp-server community S3cretC0mmunity RO
tacacs-server host 10.44.7.9 key MyTacacsKey
!
interface Vlan1
 ip address 192.168.44.10 255.255.255.0
!
line vty 0 4
 password VtyPassword123
!
end
"""


def _transcript(payload: bytes) -> Transcript:
    return Transcript(records=(Record(0.0, Direction.RX, payload),))


def _text(transcript: Transcript) -> str:
    return b"".join(record.data for record in transcript).decode("latin-1")


def test_rx_credentials_are_redacted() -> None:
    """The larger problem is not what we send -- it is what the device says.

    A running-config the device volunteered carries far more secrets than the
    tool ever transmits, and nothing marked any of it as sensitive.
    """
    result = scrub(_transcript(RUNNING_CONFIG))
    cleaned = _text(result.transcript)

    for secret in (
        "$1$mERr$3vFkYt7dQ2pLxWq8nZ1",
        "$1$abcd$efghijklmnop",
        "S3cretC0mmunity",
        "MyTacacsKey",
        "VtyPassword123",
    ):
        assert secret not in cleaned

    assert result.credentials >= 5


def test_addresses_are_anonymised_consistently() -> None:
    """Topology must stay legible without disclosing the real addressing.

    The same address always maps to the same placeholder, so a reader can still
    see which interfaces shared a subnet.
    """
    payload = b"ip address 192.168.44.10\nneighbour 192.168.44.10\npeer 10.9.9.9\n"
    cleaned = _text(scrub(_transcript(payload)).transcript)

    assert "192.168.44.10" not in cleaned
    assert "10.9.9.9" not in cleaned
    first = cleaned.splitlines()[0].split()[-1]
    second = cleaned.splitlines()[1].split()[-1]
    assert first == second


def test_hostname_is_anonymised() -> None:
    cleaned = _text(scrub(_transcript(b"hostname CORE-SW-01\n")).transcript)
    assert "CORE-SW-01" not in cleaned


def test_private_key_block_is_removed() -> None:
    payload = (
        b"crypto key\n"
        b"-----BEGIN RSA PRIVATE KEY-----\n"
        b"MIIEowIBAAKCAQEAxyz\nabcdef\n"
        b"-----END RSA PRIVATE KEY-----\n"
    )
    result = scrub(_transcript(payload))
    cleaned = _text(result.transcript)

    assert "MIIEowIBAAKCAQEAxyz" not in cleaned
    assert result.key_blocks == 1


def test_scrubbing_is_deterministic() -> None:
    """Identical input must yield byte-identical output.

    A fixture regenerated later should not produce a spurious diff, or nobody
    will trust the ones that do change.
    """
    first = scrub(_transcript(RUNNING_CONFIG)).transcript
    second = scrub(_transcript(RUNNING_CONFIG)).transcript
    assert first.records == second.records


def test_scrubbed_transcript_carries_provenance() -> None:
    result = scrub(_transcript(RUNNING_CONFIG), source="2950-session.ytx")
    assert result.transcript.public is True
    assert result.transcript.scrub_version is not None
    assert result.transcript.source == "2950-session.ytx"


def test_report_always_states_the_caveat() -> None:
    """Never promise 'secret-free'.

    Automated scrubbing cannot prove a transcript contains no sensitive data,
    and claiming otherwise is worse than not scrubbing, because it stops people
    looking.
    """
    report = scrub(_transcript(b"nothing interesting here\n")).report()
    assert "cannot prove" in report
    assert "Review before publishing" in report


def test_already_redacted_records_pass_through_untouched() -> None:
    transcript = Transcript(
        records=(Record(0.0, Direction.TX, b"<redacted>", secret=True),)
    )
    result = scrub(transcript)
    assert result.transcript.records[0].secret is True
    assert result.credentials == 0
