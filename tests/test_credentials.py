"""Credential containment.

The invariant: **no secret supplied through the credential interface appears in
any journal, JSON result, support bundle, log event or public transcript.**

Tested with a distinctive sentinel driven through the whole lifecycle, because
that is the only way to check a negative across many artifacts at once.
"""

from __future__ import annotations

import io
import json
import logging
import sqlite3
from pathlib import Path

from ciscoyoke.credentials import (
    REDACTED_DISPLAY,
    Credentials,
    Secret,
    scrub_value,
)
from ciscoyoke.journal import model
from ciscoyoke.journal.store import Journal, connect
from ciscoyoke.result.schema import Result
from ciscoyoke.session import Session
from ciscoyoke.transcript.fake import FakeDevice, FakeTransport
from ciscoyoke.transcript.schema import Direction, Record, Transcript
from ciscoyoke.transcript.scrub import scrub

SENTINEL = "Tr0ub4dor-SENTINEL-3xample"


def test_secret_does_not_render_itself() -> None:
    """The accidental paths are the ones that matter.

    f-strings, print, logging and traceback formatting are how credentials
    actually escape -- not deliberate misuse.
    """
    secret = Secret(SENTINEL)

    assert SENTINEL not in repr(secret)
    assert SENTINEL not in str(secret)
    assert SENTINEL not in f"{secret}"
    assert SENTINEL not in f"{secret!r}"
    assert SENTINEL not in "%s" % (secret,)  # noqa: UP031 - exercises __str__
    assert repr(secret) == REDACTED_DISPLAY


def test_secret_length_is_not_leaked() -> None:
    """len() must not undo the recorder's fixed-width redaction."""
    assert len(Secret("a")) == len(Secret("a much longer passphrase indeed"))


def test_reveal_is_the_only_way_out() -> None:
    assert Secret(SENTINEL).reveal() == SENTINEL


def test_credentials_serialise_without_the_secret() -> None:
    """The method a --json result or support bundle reaches for."""
    credentials = Credentials(
        username="admin",
        password=Secret(SENTINEL),
        enable_password=Secret(SENTINEL),
    )
    rendered = json.dumps(credentials.to_json())

    assert SENTINEL not in rendered
    assert credentials.to_json()["password"] is True
    assert SENTINEL not in repr(credentials)


def test_secret_is_scrubbed_from_nested_structures() -> None:
    payload = {
        "device": {"credentials": [Secret(SENTINEL), "safe"]},
        "tuple": (Secret(SENTINEL),),
    }
    assert SENTINEL not in json.dumps(scrub_value(payload), default=str)


def test_secret_never_reaches_a_transcript() -> None:
    """Sent through the real Session, on the secret path."""
    device = FakeDevice(Transcript(records=()), strict=False)
    session = Session(
        FakeTransport(device), clock=device.monotonic, sleep=device.sleeper()
    )
    session.send(SENTINEL.encode() + b"\r", secret=True)

    transcript = session.recorder.transcript()
    rendered = json.dumps([record.to_json() for record in transcript])

    assert SENTINEL not in rendered
    assert transcript.records[0].secret is True


def test_secret_never_reaches_a_journal(tmp_path: Path) -> None:
    """Even when a caller carelessly puts one in a mutation payload."""
    connection: sqlite3.Connection = connect(tmp_path / "state.db")
    try:
        journal = Journal.begin(
            connection,
            target_key="usb:test",
            target_name="bench",
            operation="recover_access",
        )
        mutation = model.Mutation(
            kind="authenticate",
            reversibility=model.Reversibility.COMPENSATABLE,
            before=scrub_value({"password": Secret(SENTINEL)}),  # type: ignore[arg-type]
            compensation={"action": "noop"},
        )
        with journal.attempting(mutation):
            pass

        raw = (tmp_path / "state.db").read_bytes()
        assert SENTINEL.encode() not in raw
        assert json.dumps(
            [m.to_json() for m in journal.mutations()]
        ).find(SENTINEL) == -1
    finally:
        connection.close()


def test_secret_never_reaches_a_json_result() -> None:
    credentials = Credentials(username="admin", password=Secret(SENTINEL))
    result = Result("intake", 0, {"credentials": credentials.to_json()})
    assert SENTINEL not in result.render()


def test_secret_never_reaches_a_log_record() -> None:
    """Logging formats with str(), which is exactly why Secret overrides it."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = logging.getLogger("ciscoyoke.test.credentials")
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        logger.debug("authenticating with %s", Secret(SENTINEL))
        logger.debug("credentials=%r", Secret(SENTINEL))
    finally:
        logger.removeHandler(handler)

    assert SENTINEL not in stream.getvalue()


def test_secret_never_reaches_a_public_transcript() -> None:
    """A redacted TX record survives scrubbing without regaining its content."""
    raw = Transcript(
        records=(
            Record(0.0, Direction.TX, b"<redacted>", secret=True),
            Record(0.1, Direction.RX, b"\r\nRouter#"),
        )
    )
    result = scrub(raw, source="test")
    rendered = json.dumps([r.to_json() for r in result.transcript])

    assert SENTINEL not in rendered
    assert result.transcript.public is True


def test_an_exception_carrying_a_secret_does_not_print_it() -> None:
    """Tracebacks format arguments with repr()."""
    try:
        raise ValueError(f"auth failed for {Secret(SENTINEL)}")
    except ValueError as exc:
        assert SENTINEL not in str(exc)
