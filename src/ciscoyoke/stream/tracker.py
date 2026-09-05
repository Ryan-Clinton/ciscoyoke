"""Contextual state tracking over accumulated signals.

Signals are context-free evidence; this is where they become a conclusion, and
where that conclusion is qualified. Every observation carries a *basis* (did we
observe this, infer it, or not know?), a *confidence*, and the evidence that
produced it -- commitment 1 of the specification, expressed in the type system
rather than in a docstring.

The tracker evaluates after **every byte**, not once per chunk. That is what
makes state detection invariant under arbitrary stream partitioning: the
sequence of states cannot depend on where a read boundary happened to fall,
because read boundaries are never consulted. It costs a tail regex scan per
byte, which is irrelevant next to a 9600-baud serial line.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

from ciscoyoke.stream.buffer import TAIL_WINDOW, StreamBuffer
from ciscoyoke.stream.signals import Signal, SignalKind, detect, first_anchored


class State(StrEnum):
    """Where the device appears to be. See specification section 5.3."""

    UNKNOWN = "unknown"
    SILENT = "silent"
    BOOTING = "booting"
    ROMMON = "rommon"
    BOOTLOADER = "bootloader"
    SETUP_DIALOG = "setup_dialog"
    PRESS_RETURN = "press_return"
    LOGIN_USERNAME = "login_username"
    LOGIN_PASSWORD = "login_password"
    ENABLE_PASSWORD = "enable_password"
    USER_EXEC = "user_exec"
    PRIV_EXEC = "priv_exec"
    CONFIG_MODE = "config_mode"
    PAGER = "pager"
    TRANSFER = "transfer"
    HUMAN_ACTION = "human_action"
    DESTRUCTIVE_RECOVERY_GUARD = "destructive_recovery_guard"


class Basis(StrEnum):
    """How a conclusion was reached. Deliberately ordinal, never numeric."""

    OBSERVED = "observed"
    INFERRED = "inferred"
    UNKNOWN = "unknown"


class Confidence(StrEnum):
    """Ordinal confidence.

    Not a probability. A float here would look scientific without being
    calibrated against anything, and would invite arithmetic nobody has earned
    the right to do. If a labelled transcript corpus ever supports real
    calibration, that is a later release and a different type.
    """

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


@dataclass(frozen=True, slots=True)
class Observation:
    """A state conclusion, with why it was reached."""

    state: State
    basis: Basis
    confidence: Confidence
    evidence: tuple[Signal, ...] = field(default=())

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.state.value} ({self.basis.value}/{self.confidence.value})"


# A prompt signal maps to exactly one state, except the two ambiguous ones
# handled separately in _resolve.
_DIRECT: dict[SignalKind, State] = {
    SignalKind.ROMMON_PROMPT: State.ROMMON,
    SignalKind.BOOTLOADER_PROMPT: State.BOOTLOADER,
    SignalKind.SETUP_DIALOG: State.SETUP_DIALOG,
    SignalKind.PAGER: State.PAGER,
    SignalKind.USERNAME_PROMPT: State.LOGIN_USERNAME,
    SignalKind.CONFIG_PROMPT: State.CONFIG_MODE,
    SignalKind.PRIV_EXEC_PROMPT: State.PRIV_EXEC,
    SignalKind.USER_EXEC_PROMPT: State.USER_EXEC,
    SignalKind.XMODEM_READY: State.TRANSFER,
}


class StateTracker:
    """Accumulates bytes and reports state transitions.

    Usage::

        tracker = StateTracker()
        tracker.note_sent(b"enable\\r")     # context for the next Password:
        for observation in tracker.feed(chunk):
            ...
    """

    def __init__(self, buffer: StreamBuffer | None = None) -> None:
        self._buffer = buffer if buffer is not None else StreamBuffer()
        self._last_sent: bytes = b""
        self._current = Observation(State.UNKNOWN, Basis.UNKNOWN, Confidence.LOW)
        self._history: list[Observation] = [self._current]

    # -- context -----------------------------------------------------------

    def note_sent(self, data: bytes) -> None:
        """Record what was transmitted, so an ambiguous prompt can be resolved.

        ``Password:`` after ``enable`` is an enable prompt; the same bytes
        arriving unprompted are a login prompt. Without this the two are
        genuinely indistinguishable, which is the whole reason signals and
        states are separate layers.
        """
        self._last_sent = data

    # -- ingestion ---------------------------------------------------------

    def feed(self, data: bytes) -> tuple[Observation, ...]:
        """Consume bytes, returning any *new* state observations.

        Evaluated one byte at a time so that the result cannot depend on how
        ``data`` was chunked. Consecutive identical states are collapsed: a
        state is reported when it is entered, not on every byte that sustains
        it.
        """
        emitted: list[Observation] = []

        for index in range(len(data)):
            self._buffer.feed(data[index : index + 1])
            observation = self._evaluate()
            if observation.state != self._current.state:
                self._current = observation
                self._history.append(observation)
                emitted.append(observation)

        return tuple(emitted)

    def _evaluate(self) -> Observation:
        tail = self._buffer.tail(TAIL_WINDOW)
        signals = detect(tail)
        return self._resolve(signals)

    def _resolve(self, signals: tuple[Signal, ...]) -> Observation:
        by_kind = {signal.kind: signal for signal in signals}

        # The guard outranks everything. If the device has announced that
        # password recovery is disabled, no prompt reading may override it --
        # this is the state that blocks destructive steps, and it must not be
        # possible to leave it by seeing something more specific afterwards.
        guard = by_kind.get(SignalKind.RECOVERY_DISABLED)
        if guard is not None:
            return Observation(
                State.DESTRUCTIVE_RECOVERY_GUARD,
                Basis.OBSERVED,
                Confidence.HIGH,
                (guard,),
            )

        anchored = first_anchored(signals)
        if anchored is not None:
            if anchored.kind is SignalKind.PASSWORD_PROMPT:
                return self._resolve_password(anchored)
            state = _DIRECT[anchored.kind]
            return Observation(state, Basis.OBSERVED, Confidence.HIGH, (anchored,))

        # No prompt: the device is mid-sentence. Boot output is still a useful
        # conclusion, but it is weaker than sitting at a prompt -- output can
        # stop at any moment and the state is only true until it does.
        booting = by_kind.get(SignalKind.BOOT_ACTIVITY)
        if booting is not None:
            return Observation(
                State.BOOTING, Basis.OBSERVED, Confidence.MEDIUM, (booting,)
            )

        press_return = by_kind.get(SignalKind.PRESS_RETURN)
        if press_return is not None:
            return Observation(
                State.PRESS_RETURN, Basis.OBSERVED, Confidence.MEDIUM, (press_return,)
            )

        return Observation(State.UNKNOWN, Basis.UNKNOWN, Confidence.LOW)

    def _resolve_password(self, signal: Signal) -> Observation:
        """Disambiguate ``Password:`` using what was last transmitted."""
        if self._last_sent.strip().lower().startswith(b"enable"):
            return Observation(
                State.ENABLE_PASSWORD,
                Basis.INFERRED,
                Confidence.HIGH,
                (signal,),
            )
        if self._last_sent:
            # Something was sent, but not `enable`. A login prompt is the
            # likelier reading, though not a certain one.
            return Observation(
                State.LOGIN_PASSWORD, Basis.INFERRED, Confidence.MEDIUM, (signal,)
            )
        # Nothing sent at all: we attached to a stream already at a prompt.
        return Observation(
            State.LOGIN_PASSWORD, Basis.INFERRED, Confidence.LOW, (signal,)
        )

    # -- accessors ---------------------------------------------------------

    @property
    def current(self) -> Observation:
        return self._current

    @property
    def history(self) -> tuple[Observation, ...]:
        return tuple(self._history)

    @property
    def states(self) -> tuple[State, ...]:
        """The deduplicated sequence of states entered, including the initial."""
        return tuple(observation.state for observation in self._history)

    @property
    def buffer(self) -> StreamBuffer:
        return self._buffer
