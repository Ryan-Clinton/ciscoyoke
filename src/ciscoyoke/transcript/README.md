# transcript

Recording console sessions, making them shareable, and replaying them as if
they were a device. This is what lets the test suite run with no hardware.

| File | What it holds |
| --- | --- |
| `schema.py` | The two formats and their loaders: `.ytx` (raw, private) and `.ytx.pub` (scrubbed, shareable). |
| `recorder.py` | Records a live session. A credential it is told about is never written. |
| `writer.py` | Incremental writing, so a recording survives a crash or a pulled cable. |
| `scrub.py` | Deterministic raw → shareable transformation, with a report of what it found. |
| `fake.py` | `FakeDevice` / `FakeTransport`: a transcript replayed as a transport. The spine of the tests. |
| `replay.py` | Runs a recording through the state tracker and shows each conclusion. |

**Entry points:** `schema.read()` / `schema.write()`, `scrub.scrub()`,
`fake.FakeTransport`.

## Invariants

- **Raw and public are different formats**, not one format with a flag. A raw
  file fails to load as a fixture.
- **Raw recordings are never committed.** `.gitignore` and CI both refuse them.
- **Scrubbing is deterministic.** The same input gives byte-identical output.
- **Scrubbing never claims a transcript is clean.** The report always says a
  person must still read it.
- **Provenance is part of a fixture.** `source` begins `hardware:` or
  `synthetic:`, and the support marks are derived from it.

## Tests

`tests/test_transcript.py`, `tests/test_scrub.py`,
`tests/test_hardware_fixtures.py`, `tests/test_capture.py`. Fixtures and their
rules: [`tests/fixtures/README.md`](../../../tests/fixtures/README.md).

## Common change: the scrubber missed something

1. Add the smallest text that shows the leak to `tests/test_scrub.py`, with
   invented values.
2. Fix the pattern in `scrub.py`.
3. Check the hardware fixtures still load and replay unchanged.
