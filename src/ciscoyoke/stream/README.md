# stream

Turns arbitrary serial bytes into a device state, with the evidence for it.

| File | What it holds |
| --- | --- |
| `buffer.py` | Byte-boundary-safe accumulation of console output. |
| `signals.py` | Stateless detectors. A signal is evidence (`Password:` was seen), never a conclusion. |
| `tracker.py` | Adds context and produces an `Observation`: `State` + `Basis` + confidence + evidence. |

**Entry point:** `StateTracker.feed(data)` in `tracker.py`.

## Invariants

- **Chunk invariance.** The states reached must not depend on where a read
  happened to split the stream. The tracker evaluates after every byte for this
  reason. Never infer anything from a read boundary.
- **Detectors stay pure.** `signals.py` is functions of a string: no I/O, no
  history. Anything that needs "what was sent just before" belongs in the
  tracker.
- **Confidence is ordinal**, never a number.
- **A live prompt outranks earlier text.** Old failure output must not hide the
  prompt the device is at now.
- **The destructive guard latches.** Once "password recovery disabled" has been
  seen it stays set, however much output follows.

## Tests

`tests/test_tracker.py`, `tests/test_chunk_invariance.py` (Hypothesis, any
partitioning of a real stream), `tests/test_hardware_fixtures.py`.

## Common change: recognise a new prompt

1. Add the pattern to `signals.py`.
2. Add a test in `tests/test_tracker.py` built from the real device text.
3. If you have a recording, `ciscoyoke transcript replay FILE` shows what the
   tracker concludes at each event and where it stalls.
