# Architecture: the interesting parts

The reasons someone should *trust* ciscoyoke, as opposed to the reasons they'd
want it. The full design is in [SPEC.md](SPEC.md); what can go wrong and which
invariant prevents it is in [THREAT-MODEL.md](THREAT-MODEL.md).

## State is inferred from an unstructured byte stream

A serial console has no connection event, no handshake and no protocol. You join
a stream already in progress and have to work out where the device is by
watching it, and by poking it only when watching has told you nothing.
Detection splits into pure, context-free *signals*
(`src/ciscoyoke/stream/signals.py`) and a contextual *tracker*
(`src/ciscoyoke/stream/tracker.py`) that weighs them into a conclusion carrying
a basis, a confidence and its evidence. `Password:` after `enable` is an enable
prompt; the same bytes arriving unprompted are a login prompt. Only the tracker,
which knows what was last sent, can tell them apart.

Real consoles are messier than the documentation. A 2950's bootloader ends lines
with LF-CR rather than CR-LF, and IOS prints log messages straight over a
question it's waiting on, so console syslog is stripped before prompt detection
(`strip_syslog`).

## Chunk-boundary correctness is a property, not a hope

Serial data arrives in arbitrary pieces, so a prompt can straddle two reads:
`Router` in one, `#` in the next. The tracker evaluates after every byte, and
Hypothesis asserts that any partition of any transcript yields the same state
sequence as the whole:

```python
@given(transcript=..., splits=st.lists(st.integers(1, 4096)))
def test_state_detection_is_chunk_invariant(transcript, splits):
    assert states(fragment(transcript, splits)) == states([transcript])
```

## The test suite needs no hardware

Sessions record to a versioned transcript format. A `FakeDevice` replays one as
a serial port and can assert the code transmits what the real session
transmitted. CI runs the real engine against real recorded device behaviour on
Windows, macOS and Linux with nothing plugged in. Transcripts from real hardware
(`tests/fixtures/hw-*.ytx.pub`) are replayed by
`tests/test_hardware_fixtures.py`. See [HARDWARE-TESTING.md](HARDWARE-TESTING.md).

## Catalyst and router recovery are different programs, not dialects

A `switch:` bootloader takes `set BAUD` and `copy xmodem: flash:<file>`; a
router's `rommon>` takes neither, carrying the rate as a flag on
`xmodem -c <file>`, and prints a differently shaped flash listing. Each has its
own driver, and a device matching neither is refused rather than handed a
procedure written for something else.

Within the Catalysts, what differs by family (when to release Mode, whether
`load_helper` exists, how long a boot takes, what console port the front panel
has) lives in `src/ciscoyoke/platform_profiles.py`, one entry per family, each
labelled `hardware`, `documentation` or `generic` by where the knowledge came
from.

## Playbooks are data before they're behaviour

Every step declares the state it may start from, the states it expects to reach,
what it destroys, and how to verify it did what it claimed
(`src/ciscoyoke/playbook/base.py`). So a plan can be checked before a byte is
sent, and a step can't pass on the prompt merely coming back: a bootloader
answers a failed `rename` with the same `switch:` as a successful one, which is
why renames and deletions are proven from a fresh flash listing.

## A transfer is not a rescue until the device boots

Image recovery ends in a boot proof with four conditions: the image loaded, IOS
reached a prompt, `show version` reports the expected image, and the console
speed was put back. Falling short of any of them reports `booted_unverified`
rather than success, because a completed transfer proves the bytes arrived, not
that they boot. The image check compares the *filename IOS reports running*, not
just the version, because the same version ships in several feature sets.

## Confidence is ordinal, never a float

`0.98` would look scientific without being calibrated against anything. Until
there is a labelled corpus, the schema carries `basis` / `confidence` /
`evidence`, which is what the tool actually knows.

## Raw and shareable transcripts are different formats

A raw `.ytx` is private by default and *fails to load* as a test fixture; only a
scrubbed `.ytx.pub` with recorded provenance can be committed, and CI refuses a
raw one. Scrubbing joins a stream's serial reads back together before matching
(a secret can straddle two), removes credentials, banners, addresses, hostnames,
serial numbers and MAC addresses, reports what it found, and says plainly that
it cannot prove a transcript is clean. `ciscoyoke report` goes further and cuts
configuration output out entirely before scrubbing starts.
