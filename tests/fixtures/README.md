# Transcript fixtures

Every file here is a `.ytx.pub` — a scrubbed, shareable transcript with recorded
provenance. Raw `.ytx` recordings are private by definition and are refused by
both `.gitignore` and CI.

## Provenance is part of the fixture

Each transcript's `source` field states where its bytes came from, and the
support matrix in the README derives its marks from that field. There are two
kinds, and conflating them would make the entire evidence model dishonest:

| `source` prefix | Meaning | Earns |
| --- | --- | --- |
| `hardware:` | Captured from a real device by a maintainer, then scrubbed | ● hardware verified |
| `synthetic:` | Constructed from Cisco's published output. **Never touched a device.** | ○ documentation-derived |

A synthetic fixture is still worth having: it pins the parser and the state
machine against the output shape Cisco documents, and it fails loudly if a
refactor changes behaviour. What it cannot do is prove the tool works on real
hardware, because no hardware was involved. It therefore never earns a ● or a ◐.

**Current status:** the `hw-*` files are `hardware:`, captured from a
WS-C2950G-24-EI and replayed by `tests/test_hardware_fixtures.py`. Everything
else is `synthetic:`.

## From a user's report

A `ciscoyoke report` zip already holds a scrubbed `transcript.ytx.pub` with
configuration output removed. Run `ciscoyoke transcript replay` on it to see
where the tracker lost the thread, commit it here as `hw-<model>-<what>.ytx.pub`
with a `hardware:` source naming the reporter's model, write the test that
replays it and fails, then fix the code until it passes.

## Adding a real capture

Destructive commands record every run to the state directory
(`%LOCALAPPDATA%\ciscoyoke\transcripts` on Windows); `capture` and `console
--record` write wherever you point them.

```
ciscoyoke transcript scrub RAW.ytx -o tests/fixtures/hw-NAME.ytx.pub \
    --source "hardware: MODEL, what the run did, DATE"
```

Then **read the scrubbed file before committing it.** The scrub report says what
it redacted; it cannot say what it missed. The first real captures proved the
point: the scrubber missed a RADIUS key written with `auth-port`/`acct-port`
options and a banner naming the previous owner and their street address, both
since fixed. No scrubber can know that a config's prose identifies somebody.

**Never commit a run that read a previous owner's configuration** --
`reset` archiving a stale file, `archive`, a `show running-config` -- however
well it scrubs. Record the same operation against a lab-only configuration
instead.
