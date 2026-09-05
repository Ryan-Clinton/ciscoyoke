# ciscoyoke

[![CI](https://github.com/Ryan-Clinton/ciscoyoke/actions/workflows/ci.yml/badge.svg)](https://github.com/Ryan-Clinton/ciscoyoke/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![Licence: MIT](https://img.shields.io/badge/licence-MIT-green.svg)](LICENSE)
[![Status: pre-alpha](https://img.shields.io/badge/status-pre--alpha-orange.svg)](#status)

**A rescue bench for old Cisco hardware.**
Discover, diagnose, preserve, recover and commission Cisco gear from the serial
console — with no management IP.

```
   unknown device                    ciscoyoke scan
         ↓                                 ↓
   bootloader found      ──►    archive whatever can be preserved
         ↓                                 ↓
   no bootable image     ──►    guided recovery, image supplied by you
         ↓                                 ↓
   IOS boots, version proven      lab verify catches the wrong cable
```

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

**Pre-alpha.** Every command in [the specification](docs/SPEC.md) exists and is
tested. **None of it has met a Cisco device** — see
[hardware support](#hardware-support), which is the honest limit on all of it.

Read-only — safe against hardware in unknown condition:

| | |
| --- | --- |
| `ciscoyoke doctor` | host serial, permission and TFTP-bind diagnostics |
| `ciscoyoke ports` | USB adapter identity and how stable it is |
| `ciscoyoke scan` | identify every attached device, passively |
| `ciscoyoke intake PORT` | identify one device without changing it |
| `ciscoyoke rescue PORT` | **the front door** — diagnose, preserve, recommend |
| `ciscoyoke health PORT` | POST, flash, memory and config-register checks |
| `ciscoyoke archive PORT` | preserve what the device will disclose |
| `ciscoyoke transcript scrub` | raw recording → shareable fixture |

Destructive — leased, journalled, dry-run by default, `--confirm` required:

| | |
| --- | --- |
| `ciscoyoke reset PORT` | erase to a known-empty baseline |
| `ciscoyoke recover access PORT` | platform-aware password recovery, including the guided Mode-button sequence |
| `ciscoyoke recover image PORT --image F` | XMODEM rescue for a device with no bootable image, ending in boot proof |
| `ciscoyoke lab apply LABFILE` | push per-device configuration |
| `ciscoyoke login PORT` | authenticate with credentials you already have |

Plus `ciscoyoke console`, `ciscoyoke resolve` (reconcile an interrupted
recovery against the device) and `ciscoyoke support-bundle`.

**Deliberately not implemented: TFTP image delivery.** The provider, its
constraints and the ROMMON variable generation exist, but the command does not
collect the addressing a ROMMON TFTP boot needs, so `--via tftp` refuses with an
explanation rather than half-configuring a stranded device. `--via xmodem` is
the working path.

**One thing is deliberately not implemented: TFTP image delivery.** The provider,
the constraints and the ROMMON variable generation all exist, but the command
does not collect the addressing a ROMMON TFTP boot needs, so `--via tftp` refuses
with an explanation rather than half-configuring a device. `--via xmodem` is the
working path.

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

**A transfer is not a rescue until the device boots.** Image recovery ends in a
boot proof with four conditions: the image loaded, IOS reached a prompt, `show
version` reports the expected image, and the console speed was put back. Falling
short of all four reports `booted_unverified` rather than success — a completed
transfer proves the bytes arrived, not that they boot.

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
