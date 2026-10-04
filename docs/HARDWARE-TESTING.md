# Hardware: what's verified, how, and how to help

## The marks

```
● Hardware verified      run against a real device by a maintainer; the recording is committed
◐ Replay verified        passes against a committed real transcript, no hardware run of this version
○ Documentation-derived  implemented from Cisco's published procedure, never executed
— Not yet attempted
```

A mark is only ever as strong as its evidence, and every ● links to the
recording that earned it. The distinction matters: most "supports Cisco
Catalyst" claims mean "I saw a command in a manual that probably works".

| Platform | Intake | Access recovery | Image rescue | Reset |
| --- | --- | --- | --- | --- |
| Catalyst 2950 | ● [cold boot](../tests/fixtures/hw-switch-2950-cold-boot-locked.ytx.pub) | ● [no-restore](../tests/fixtures/hw-switch-2950-recover-no-restore.ytx.pub) | — | ● [lab config](../tests/fixtures/hw-switch-2950-reset-lab-config.ytx.pub) ¹ |
| Catalyst 2960 | ○ | ○ | — | ○ |
| Catalyst 2960-S / X / Plus | ○ | ○ | — | ○ |
| Catalyst 3560 / 3560G / 3560V2 | ○ | ○ | — | ○ |
| Catalyst 3550 / 3750 / 3560-X / 3560-CX | — | — ² | — | — ² |
| Cisco 1760 | — | — | — | — |
| Cisco 1800 / 1841 | — | — | — | — |

¹ Recorded against a made-up configuration loaded for the purpose. The first
reset on this switch read a previous owner's configuration into the archive
before deleting it; that recording is not ours to publish and never will be.

² No model-specific profile: these get Cisco's general procedure with longer
waits, and destructive commands ask for `--accept-unverified`.

## Where the ○ rows come from

`src/ciscoyoke/platform_profiles.py` holds one entry per Catalyst family (when
to release Mode, whether `load_helper` exists, how long a boot takes, what
console port the front panel has) and names the Cisco document each entry was
taken from. A 2960 is told to release Mode once SYST has gone amber and then
green, not the 2950's "when STAT goes out", and the 2960-S/X/Plus skips
`load_helper`, which its bootloader doesn't have. The tool shows how well it
knows your switch before anything runs.

## Console cables

For the usual Cisco RJ-45 console port, prefer a USB-to-RJ45 rollover cable built
around an FTDI USB-serial adapter **that reports a USB serial number**. ciscoyoke
can then recognise the same physical cable after it is moved to another USB
socket instead of relying on an operating-system port name such as `COM3` or
`/dev/ttyUSB0`.

A few common alternatives need more care:

- **Prolific PL2303 cables:** many clone or older chips are refused by current
  Windows drivers with Device Manager **Code 10** / “PL2303HXA phased out”.
  A port can appear present while the console still produces no useful traffic,
  so suspect the cable before blaming baud rate or the switch.
- **CH340 cables:** they can work, but commonly expose no USB serial number.
  ciscoyoke therefore has to identify them by physical USB topology; moving the
  cable to another socket can change its identity.
- **DB9 rollover cable + USB-serial adapter:** this is fine when the USB adapter
  is reliable. The same identity rule applies: an adapter with a stable USB
  serial number is easier to recognise safely across reconnects.
- **Catalyst 2960-S / 2960-X USB console:** these models also provide Cisco's
  mini-B USB console interface. Windows needs Cisco's USB console driver. When
  that USB console is connected, the switch's RJ-45 console port is disabled,
  so do not troubleshoot the RJ-45 side as if both ports were active together.

Run `ciscoyoke doctor` before a recovery. It reports known adapter caveats from
the USB descriptor, but it cannot prove that a particular cable is electrically
sound or genuine. If the console remains silent, swap in a known-good cable
before changing recovery steps.

## Testing one you own

1. `ciscoyoke doctor`: the cable and adapter. Prolific clones are the usual
   cause of "nothing at all". On a 2960-S/X's USB console port, Windows needs
   Cisco's USB console driver.
2. `ciscoyoke scan`, then `ciscoyoke rescue PORT`. These are read-only.
3. If you want to go further, the destructive commands are dry runs first.
   Read what they plan to do.
4. Whatever happens, especially if it fails: `ciscoyoke report`, read the
   transcript inside the zip, and attach it to a
   [hardware report](https://github.com/Ryan-Clinton/ciscoyoke/issues/new?template=hardware-report.yml)
   with the model from the label and what the LEDs did.

## From a report to a fix

```
real Cisco hardware
       │
       ▼
serial transcript        every destructive run records itself
       │
       ▼
ciscoyoke report         scrubbed; configuration output removed
       │
       ▼
transcript replay        shows where the tracker lost the thread
       │
       ▼
fixture + failing test   tests/fixtures/hw-*.ytx.pub
       │
       ▼
fix, and CI replays it on every commit from then on
```

`ciscoyoke transcript replay FILE` prints every state the tracker concluded,
the evidence for each, and the prompt it stalled on. The transcript is committed
as a `hardware:` fixture (see [tests/fixtures/README.md](../tests/fixtures/README.md)),
a test that replays it is written to fail, and the code is fixed until it
passes. That's the loop that turned the 2950's first failures into
`tests/test_hardware_fixtures.py`.

Never commit a recording that read somebody's configuration, however well it
scrubs. Record the same operation against a lab-only configuration instead.
