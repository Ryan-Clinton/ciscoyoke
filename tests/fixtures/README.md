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

**Current status: every fixture in this directory is `synthetic:`.** No Cisco
device has been connected to this code. The support matrix is entirely `—`
for exactly that reason, and the first `●` will appear when a real 1760 or 2950
transcript is captured and committed.

## Adding a real capture

```
ciscoyoke console COM3 --record session.ytx     # raw, private, gitignored
ciscoyoke transcript scrub session.ytx          # produces session.ytx.pub
```

Then read the scrubbed file before committing it. The scrub report says what it
redacted; it cannot say what it missed.
