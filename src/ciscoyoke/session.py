"""The expect/send loop: I/O, deadlines, wake behaviour and pager handling.

This is where the pure state machinery meets a transport. It owns the only
thing the tracker cannot know on its own -- the passage of time -- and it is
where *silence* becomes a conclusion rather than a hang.

Passive-first, deliberately. A session attached to an unknown device reads
before it writes, because a device mid-boot will tell you what it is if you let
it, and sending a carriage return into a ROMMON transfer or a boot sequence can
change the outcome. Only when the stream has been quiet is the device nudged.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable

from ciscoyoke.stream.tracker import Observation, State, StateTracker
from ciscoyoke.transcript.recorder import Recorder
from ciscoyoke.transport.base import Transport

# How long the stream must be quiet before the device is considered settled.
QUIESCENCE = 0.35

# How long to listen passively before concluding an apparently silent device
# has nothing to say.
PASSIVE_LISTEN = 1.5

# Ceiling on following a device that is actively talking. A cold boot to a
# prompt is ordinarily 20-40s on this vintage of hardware; a reload can be
# longer, which is why the ceiling is generous rather than tight.
BOOT_FOLLOW = 120.0

# How long a nudged device gets to say anything at all.
WAKE_TIMEOUT = 3.0


class SessionTimeoutError(TimeoutError):
    """A state was awaited that did not arrive in time."""

    def __init__(self, expected: Iterable[State], observed: Observation) -> None:
        self.expected = tuple(expected)
        self.observed = observed
        names = ", ".join(state.value for state in self.expected)
        super().__init__(
            f"waited for {names}; observed {observed.state.value} "
            f"({observed.basis.value}/{observed.confidence.value})"
        )


class Session:
    """Drives one console endpoint.

    ``clock`` and ``sleep`` are injected so a replayed transcript can run in
    virtual time -- a four-minute reload tests in milliseconds without the code
    under test knowing the difference.
    """

    def __init__(
        self,
        transport: Transport,
        *,
        recorder: Recorder | None = None,
        tracker: StateTracker | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._transport = transport
        self._recorder = recorder if recorder is not None else Recorder(clock=clock)
        self._tracker = tracker if tracker is not None else StateTracker()
        self._clock = clock
        self._sleep = sleep
        self._last_rx_at = clock()

    # -- accessors ---------------------------------------------------------

    @property
    def transport(self) -> Transport:
        """The underlying endpoint.

        Exposed because a few operations are genuinely transport-level rather
        than stream-level -- asserting a break, changing line speed -- and those
        need the transport itself, not the byte stream over it.
        """
        return self._transport

    @property
    def tracker(self) -> StateTracker:
        return self._tracker

    @property
    def recorder(self) -> Recorder:
        return self._recorder

    @property
    def state(self) -> Observation:
        return self._tracker.current

    # -- I/O ---------------------------------------------------------------

    def pump(self) -> bytes:
        """Read once, record it, and feed the tracker."""
        data = self._transport.read()
        if data:
            self._recorder.record_rx(data)
            self._tracker.feed(data)
            self._last_rx_at = self._clock()
        return data

    def send(self, data: bytes, *, secret: bool = False) -> None:
        """Transmit, recording it and telling the tracker what was sent.

        ``secret`` routes the payload to the recorder's redacting path, so a
        credential never reaches the transcript. The tracker still learns what
        *kind* of thing was sent, which is what disambiguates the reply.
        """
        self._tracker.note_sent(data)
        if secret:
            self._recorder.record_secret_tx(data)
        else:
            self._recorder.record_tx(data)
        self._transport.write(data)

    # -- waiting -----------------------------------------------------------

    def settle(self, quiescence: float = QUIESCENCE, timeout: float = 5.0) -> Observation:
        """Read until the device stops talking.

        Quiescence is what makes an anchored prompt trustworthy: a prompt
        pattern at the end of a stream that is still flowing may just be a line
        of output that has not finished yet.
        """
        deadline = self._clock() + timeout
        self._last_rx_at = self._clock()

        while self._clock() < deadline:
            if not self.pump() and self._clock() - self._last_rx_at >= quiescence:
                break
            self._sleep(0.01)

        return self._tracker.current

    def wait_for(
        self,
        states: Iterable[State],
        *,
        timeout: float = 30.0,
        after_offset: int | None = None,
    ) -> Observation:
        """Read until one of ``states`` is reached, or fail loudly.

        Never returns a state that was not observed. A timeout carries what was
        seen instead, because "expected X, got Y" is diagnosable and "timed out"
        is not.

        ``after_offset`` demands that the conclusion rest on bytes arriving
        after that position in the stream. Without it, waiting for a state the
        session is *already* in returns immediately and proves nothing about
        whether the device reacted -- fine when merely confirming where things
        stand, wrong after sending a command.
        """
        wanted = set(states)
        deadline = self._clock() + timeout

        def satisfied() -> bool:
            if self._tracker.current.state not in wanted:
                return False
            if after_offset is None:
                return True
            return len(self._tracker.buffer) > after_offset

        while self._clock() < deadline:
            if satisfied():
                return self._tracker.current
            self.pump()
            self._sleep(0.01)

        if satisfied():
            return self._tracker.current
        raise SessionTimeoutError(wanted, self._tracker.current)

    def drain_pager(self, limit: int = 200) -> None:
        """Page through ``--More--`` prompts until the output ends.

        ``limit`` bounds it: a device that keeps producing pages forever should
        stop the tool rather than hang it.
        """
        for _ in range(limit):
            if self._tracker.current.state is not State.PAGER:
                return
            self.send(b" ")
            self.settle()

    # -- discovery ---------------------------------------------------------

    def observe_passively(
        self,
        listen: float = PASSIVE_LISTEN,
        *,
        follow: float = BOOT_FOLLOW,
    ) -> Observation:
        """Listen without transmitting anything.

        The safest possible probe, and often sufficient: a booting device
        narrates itself. Nothing here can change the device's state, so it is
        the correct first move against hardware whose condition is unknown.

        Listening is not a fixed window. A device mid-boot goes on talking for
        far longer than any sensible default -- twenty seconds to a prompt is
        ordinary, and a reload is minutes -- so once output is flowing this
        *follows* it rather than giving up partway through a boot and reporting
        the resulting silence as an unknown device.

        Returns as soon as a conclusive state is reached, on quiescence, or at
        the ``follow`` ceiling, whichever comes first.
        """
        started = self._clock()
        self._last_rx_at = started

        while True:
            now = self._clock()
            if self._is_conclusive():
                return self._tracker.current
            if now - started >= follow:
                return self._tracker.current

            received = self.pump()
            if not received:
                quiet_for = self._clock() - self._last_rx_at
                # Past the initial window with nothing arriving: the device has
                # either finished or was never talking. Either way, more
                # listening tells us nothing.
                if now - started >= listen and quiet_for >= QUIESCENCE:
                    return self._tracker.current
            self._sleep(0.01)

    def _is_conclusive(self) -> bool:
        """Whether the current state is worth stopping on.

        ``BOOTING`` deliberately is not: it means output is flowing, and the
        interesting answer is whatever the device settles at.
        """
        return self._tracker.current.state not in (
            State.UNKNOWN,
            State.SILENT,
            State.BOOTING,
        )

    def wake(self, timeout: float = WAKE_TIMEOUT) -> Observation:
        """Nudge a silent device with a carriage return.

        Only called after passive observation found nothing. A bare CR is the
        least destructive thing that can be sent to a console: at a prompt it
        redraws it, at a login it fails harmlessly, and in the setup dialog it
        accepts a default that a subsequent ``no`` reverses.
        """
        self.send(b"\r")
        deadline = self._clock() + timeout
        while self._clock() < deadline:
            if self.pump():
                return self.settle()
            self._sleep(0.02)
        return self._tracker.current

    def is_silent(self) -> bool:
        """Whether listening has produced no conclusion.

        Keyed on what was *learned*, not on whether bytes arrived. A device that
        emitted a stray newline and then nothing is, for identification
        purposes, exactly as uninformative as one that emitted nothing at all --
        and both want the same next move, which is a gentle nudge.
        """
        return not self._is_conclusive()
