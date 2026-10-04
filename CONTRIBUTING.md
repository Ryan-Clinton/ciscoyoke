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

### Your first 15 minutes

```bash
git clone https://github.com/Ryan-Clinton/ciscoyoke
cd ciscoyoke
python -m venv .venv
.venv/bin/pip install -e ".[dev]"        # Windows: .venv\Scripts\pip
.venv/bin/python tools/check.py           # Windows: .venv\Scripts\python
```

If that prints four `ok` lines, your environment is ready. Nothing needs to be
plugged in: the suite runs against recorded device behaviour.

```text
ok   ruff
ok   mypy
ok   pytest (429 passed in 41s)
ok   fixture safety (no raw transcripts committed)
```

Then make one harmless change to see the loop work. In `cmd_doctor` in
`src/ciscoyoke/cli.py`, change the heading `"Host diagnostics"` to anything you
like and run:

```bash
.venv/bin/pytest tests/test_cli.py
```

Still green: the tests pin the contract (exit codes, the JSON shape, the advice
given), not the prose. Now delete `--via xmodem` from the advice a few lines
below and run it again. `test_doctor_offers_the_linux_capability_only_on_linux`
fails and tells you which promise you broke. Put both back. That is the whole
workflow.

`tools/check.py` is the one command that says a change is ready, and CI runs
the same script. `python tools/check.py test` (or `lint`, `types`, `fixtures`)
runs one part.

### Finding your way around

- [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md): **where do I change X?** A table
  from the thing you want to change to the files and tests involved.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): why it is built this way.
- [docs/SPEC.md](docs/SPEC.md): the whole design.
- `stream/`, `playbook/`, `transcript/` and `image/` each have a short
  `README.md` listing the invariants a change must keep.

### What a good change looks like

- **One complete idea per pull request.** A 40-line PR that fully solves one
  problem is better than a 600-line PR solving six. A platform profile is a PR.
  A tracker state is a PR. A documentation fix is a PR.
- **Evidence for device behaviour.** Say where a claim about hardware came
  from: a recording, a Cisco document (link it), or a synthetic case. The pull
  request template asks.
- **A test built from real device output** where there is any, for a change to
  how the tool reads a console. `ciscoyoke transcript replay` shows what the
  tracker makes of a recording.
- **Test names state a behaviour or invariant in plain English**, and tests
  live beside the behaviour they protect:
  `test_3560_profile_does_not_claim_cx_or_x_families`, not `test_case_4` or
  `test_fix_from_review`. The name should tell a future reader which rule
  broke, not when the bug was found.
- **Comments explain why**, not what. There is no need to add a docstring to
  every function.

### AI-assisted contributions

Welcome. The contributor remains responsible for understanding the change,
identifying its evidence, and responding to review.

## How review works

Reviews optimise for correctness, evidence and maintainability. Expect to be
asked for changes where:

- a claim is wider than its evidence (a profile prefix that also matches a
  family the cited document does not cover);
- a destructive operation cannot be shown to be safe;
- the change makes the next device family harder to add.

Comments marked **nit:** are optional polish; take them or leave them. A change
does not have to fix unrelated problems before it can merge, and it does not
have to be perfect: it has to leave the project better than it found it.

Review comes from the maintainer, usually within a few days. There is no
`CODEOWNERS` file yet because there is one maintainer; it will be added when an
area has a second.

## Security

Never put credentials, raw transcripts or configuration backups in a public
issue. See [SECURITY.md](SECURITY.md) for what counts and how to report a
vulnerability privately.

Be decent to each other: [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
