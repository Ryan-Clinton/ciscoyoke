# ciscoyoke

**A rescue bench for old Cisco hardware.**
Discover, diagnose, preserve, recover and commission Cisco gear from the serial
console — with no management IP.

> Take an unknown Cisco box from "pulled from a rack / bought second-hand" to a
> known-good, documented lab node — from the serial console, with no management
> IP required.

---

## The gap

Every network automation tool in common use — netmiko, scrapli, Nornir, NAPALM,
Ansible — connects over SSH or telnet, and assumes the device already has an IP
address, a reachable management interface, and credentials that work.

A second-hand Cisco device has none of those. It arrives with a stranger's
configuration, an unknown enable password, an unverified IOS version, possibly
no working boot image at all. Everything between "box arrives from eBay" and
"normal automation can reach it" is done by hand: a console cable, a terminal
emulator, and a Cisco tech note from 2007 in another window.

```
   eBay box  ──►  [ ciscoyoke ]  ──►  device has an IP  ──►  [ netmiko, Nornir, Ansible ]
                  console/serial                              SSH / telnet
                  no IP required                              IP required
```

ciscoyoke hands off the moment a device has an address. It is not trying to be
netmiko, and it is not trying to be ConsolePi.

## Status

**Pre-alpha.** The read-only half of P0 is implemented and tested:

| | |
| --- | --- |
| `ciscoyoke doctor` | host serial, permission and TFTP-bind diagnostics |
| `ciscoyoke ports` | USB adapter identity and how stable it is |
| `ciscoyoke scan` | identify every attached device, passively |
| `ciscoyoke intake PORT` | identify one device without changing it |
| `ciscoyoke transcript scrub` | raw recording → shareable fixture |

**Nothing yet writes to a device.** `intake` reads, listens, and at most sends a
bare carriage return to wake a silent console; it never changes configuration.
The commands that mutate hardware — `archive`, `recover`, `reset`, `rescue` —
land with the recovery journal and the transport lease, and not before. So do
image rescue and the lab topology verbs.

Roughly a third of [the specification](docs/SPEC.md) is built. The unbuilt part
is deliberately the part that can break a device.

## Hardware support

Nothing is claimed until it is earned — and nothing has been earned yet.

**No Cisco device has ever been connected to this code.** The committed
transcript fixtures are `synthetic:` provenance: constructed from Cisco's
published output to pin the parsers and the state machine, never captured from
hardware. They are useful, and they are not evidence about real behaviour. See
[tests/fixtures/README.md](tests/fixtures/README.md).

```
● Hardware verified      run against real hardware by a maintainer
◐ Replay verified        passes against a committed transcript, no hardware run
○ Documentation-derived  implemented from official docs, never executed
— Not yet attempted
```

| Platform | Intake | Access recovery | Image rescue | Reset |
| --- | --- | --- | --- | --- |
| Cisco 1760 | — | — | — | — |
| Catalyst 2950 | — | — | — | — |
| Catalyst 3550 | — | — | — | — |
| Catalyst 2960 | — | — | — | — |

Every ● and ◐ will link to the transcript fixture that earned it.

## The interesting parts

**State is inferred from an unstructured byte stream.** A serial console has no
connection event, no handshake and no protocol — you join a stream already in
progress and have to work out where the device is by poking it. Detection splits
into pure context-free *signals* and a contextual *tracker* that weighs them
into a conclusion carrying a basis, a confidence and its evidence.

**Chunk-boundary correctness is a property, not a hope.** Serial data arrives in
arbitrary pieces, so a prompt can straddle two reads — `Router` in one, `#` in
the next. The tracker evaluates after every byte, and Hypothesis asserts that
any partition of any transcript yields the same state sequence as the whole:

```python
@given(transcript=..., splits=st.lists(st.integers(1, 4096)))
def test_state_detection_is_chunk_invariant(transcript, splits):
    assert states(fragment(transcript, splits)) == states([transcript])
```

**The test suite needs no hardware.** Sessions record to a versioned transcript;
a `FakeDevice` replays one as a serial port and asserts the code transmits what
the real session transmitted. CI runs the real engine against real recorded
device behaviour on a machine with nothing plugged in.

**Confidence is ordinal, never a float.** `0.98` would look scientific without
being calibrated against anything. Until there is a labelled corpus, the schema
carries `basis` / `confidence` / `evidence`, which is what the tool actually
knows.

**Raw and shareable transcripts are different formats.** A raw `.ytx` is private
by default and *fails to load* as a test fixture; only a scrubbed `.ytx.pub`
with recorded scrub provenance can be committed. Scrubbing reports what it
found and states plainly that it cannot prove a transcript is clean.

## Install

```bash
pipx install ciscoyoke     # not yet published
```

From a clone:

```bash
python -m venv .venv && .venv/bin/pip install -e ".[dev]"
.venv/bin/pytest
```

## Documentation

- [Specification](docs/SPEC.md) — the design, its commitments, and what was
  deliberately refused
- [Threat model](docs/THREAT-MODEL.md) — what can go wrong and which invariant
  prevents it

## Firmware

ciscoyoke **never hosts, mirrors, searches for or redistributes Cisco IOS
images**, and no feature accepts a URL to fetch one from. It accepts a
user-supplied image and automates transport and verification. Lawful
entitlement to any image is the user's responsibility.

## Licence

MIT.
