# Contributing

**You don't need to write Python to help.** ciscoyoke's weakest point is the
hardware nobody here owns, and the most valuable contribution is evidence from
yours.

## Ways to help, roughly in order of value

1. **Test on hardware you own.** Anything in the
   [wanted table](README.md#hardware-tested-and-wanted), or anything not in it.
   `ciscoyoke rescue PORT` is read-only and a good start.
2. **Send a report when it fails, or when it works on something new.**
   `ciscoyoke report` builds one zip: the run scrubbed, with configuration output
   removed. Read the transcript inside, then open a
   [hardware report](https://github.com/Ryan-Clinton/ciscoyoke/issues/new?template=hardware-report.yml).
   Include the model from the label and what the LEDs did. No recording holds
   either.
3. **Add a platform profile from Cisco's documentation.** One entry in
   `src/ciscoyoke/platform_profiles.py`: when to release Mode, whether the
   bootloader has `load_helper`, the console port, and the link to the Cisco
   document you took it from. It's marked `documentation` until someone runs it.
4. **Improve detection.** A prompt the tool didn't recognise is usually one
   pattern in `src/ciscoyoke/stream/signals.py` plus a test built from the real
   text.
5. **Docs**: anything that confused you is a bug.
6. **Code**: see below.

Issues labelled [good first issue](https://github.com/Ryan-Clinton/ciscoyoke/labels/good%20first%20issue)
and [help wanted](https://github.com/Ryan-Clinton/ciscoyoke/labels/help%20wanted)
are the places to start.

**Issues aren't assigned.** There's no need to ask to work on one: open a pull
request (a draft is fine, early is welcome) and it'll be reviewed. If two land
for the same issue, the first one that's ready gets merged. Hardware issues are
open to anyone who owns the device.

## Two rules that aren't negotiable

- **Never commit a raw recording** (`.ytx`), and never commit one that read
  somebody's configuration, however well it scrubs. CI refuses raw ones; the
  second is on you. See [tests/fixtures/README.md](tests/fixtures/README.md).
- **Never claim support that isn't earned.** A ● needs a committed hardware
  recording; documentation-derived work is ○. See
  [docs/HARDWARE-TESTING.md](docs/HARDWARE-TESTING.md).

## Working on the code

```bash
git clone https://github.com/Ryan-Clinton/ciscoyoke
cd ciscoyoke
python -m venv .venv
.venv/bin/pip install -e ".[dev]"        # Windows: .venv\Scripts\pip
.venv/bin/python tools/check.py           # Windows: .venv\Scripts\python
```

`tools/check.py` is the one command that says a change is ready. It runs ruff,
mypy, the test suite and the raw-recording check, and CI runs the same script:

```text
ok   ruff
ok   mypy
ok   pytest (429 passed in 41s)
ok   fixture safety (no raw transcripts committed)
```

`python tools/check.py test` (or `lint`, `types`, `fixtures`) runs one part.

The suite runs against recorded device behaviour, so nothing needs to be plugged
in. A change to how the tool reads a console should come with a test built from
real device output where there is any: `ciscoyoke transcript replay` shows what
the tracker makes of a recording.

[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) is the quickest orientation;
[docs/SPEC.md](docs/SPEC.md) is the whole design.

Be decent to each other: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
