"""The property the whole detection design exists to guarantee.

Serial data arrives in arbitrary chunks. A prompt can straddle a read boundary
-- ``Router`` in one read, ``#`` in the next -- and a detector that inspects
each read in isolation will miss it and then hang until timeout. That is the
single most common defect in hand-rolled console scripts.

Pinning it with a property rather than a handful of examples is the difference
between a script and a tool: Hypothesis will find the split that lands between
``Router`` and ``#``, and it will find the ones nobody thought of.
"""

from __future__ import annotations

from hypothesis import given, settings
from hypothesis import strategies as st

from ciscoyoke.stream.tracker import State, StateTracker
from ciscoyoke.transcript.fake import chunked

# Real console exchanges, in the shapes a device actually emits them.
TRANSCRIPTS: tuple[bytes, ...] = (
    b"\r\nRouter>",
    b"\r\nRouter#",
    b"\r\nRouter(config)#",
    b"\r\nRouter>enable\r\nPassword: ",
    b"\r\nswitch: ",
    b"\r\nrommon 1 > ",
    b"\r\n--More-- ",
    b"\r\nWould you like to enter the initial configuration dialog? [yes/no]: ",
    b"System Bootstrap, Version 12.2(8r)\r\nSelf decompressing the image\r\n",
    (
        b"System Bootstrap, Version 12.2\r\n"
        b"Initializing flashfs...\r\n"
        b"Press RETURN to get started!\r\n\r\n"
        b"Router>"
    ),
    (
        b"\r\nRouter>enable\r\n"
        b"Password: \r\n"
        b"Router#configure terminal\r\n"
        b"Router(config)#"
    ),
    (
        b"WARNING: PASSWORD RECOVERY FUNCTIONALITY IS DISABLED\r\n"
        b"switch: "
    ),
)


def states_for(pieces: list[bytes]) -> tuple[State, ...]:
    tracker = StateTracker()
    for piece in pieces:
        tracker.feed(piece)
    return tracker.states


@settings(max_examples=400, deadline=None)
@given(
    index=st.integers(min_value=0, max_value=len(TRANSCRIPTS) - 1),
    splits=st.lists(st.integers(min_value=1, max_value=4096), max_size=24),
)
def test_state_detection_is_chunk_invariant(index: int, splits: list[int]) -> None:
    """Chunking must not change the sequence of states observed."""
    transcript = TRANSCRIPTS[index]
    whole = states_for([transcript])
    fragmented = states_for(chunked(transcript, splits))
    assert fragmented == whole


@settings(max_examples=200, deadline=None)
@given(
    index=st.integers(min_value=0, max_value=len(TRANSCRIPTS) - 1),
)
def test_byte_at_a_time_matches_whole(index: int) -> None:
    """The pathological chunking -- one byte per read -- is still equivalent."""
    transcript = TRANSCRIPTS[index]
    per_byte = states_for([transcript[i : i + 1] for i in range(len(transcript))])
    assert per_byte == states_for([transcript])


def test_the_specific_split_that_breaks_naive_implementations() -> None:
    """``Router`` then ``#``: the exact boundary a per-read regex misses."""
    tracker = StateTracker()
    tracker.feed(b"\r\nRouter")
    assert tracker.current.state is State.UNKNOWN

    tracker.feed(b"#")
    assert tracker.current.state is State.PRIV_EXEC


@settings(max_examples=200, deadline=None)
@given(splits=st.lists(st.integers(min_value=1, max_value=200), max_size=16))
def test_guard_survives_any_chunking(splits: list[int]) -> None:
    """The recovery-disabled guard must never be lost to a read boundary.

    This is the invariant with teeth: the guard is what blocks destructive
    steps, so a chunking that hid it would silently disable the tool's most
    important refusal.
    """
    stream = (
        b"WARNING: PASSWORD RECOVERY FUNCTIONALITY IS DISABLED\r\nswitch: "
    )
    tracker = StateTracker()
    for piece in chunked(stream, splits):
        tracker.feed(piece)
    assert tracker.current.state is State.DESTRUCTIVE_RECOVERY_GUARD
