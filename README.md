# ciscoyoke

[![CI](https://github.com/Ryan-Clinton/ciscoyoke/actions/workflows/ci.yml/badge.svg)](https://github.com/Ryan-Clinton/ciscoyoke/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![Licence: MIT](https://img.shields.io/badge/licence-MIT-green.svg)](https://github.com/Ryan-Clinton/ciscoyoke/blob/main/LICENSE)
[![PyPI](https://img.shields.io/pypi/v/ciscoyoke.svg)](https://pypi.org/project/ciscoyoke/)
[![Status: early alpha](https://img.shields.io/badge/status-early%20alpha-orange.svg)](#hardware-tested-and-wanted)

**Rescue old Cisco hardware from the serial console.**

Bought a Catalyst on eBay? Inherited a switch with somebody else's password?
Found one in a store room that nobody has the login for? Got a `switch:` prompt
and no working IOS? Not even sure which COM port the cable is on?

```
   unknown / locked / broken                       known-good lab device
            │                                               ▲
            └──── console cable ──►  ciscoyoke  ────────────┘
                                   no IP address needed
```

<p align="center">
  <img src="https://raw.githubusercontent.com/Ryan-Clinton/ciscoyoke/main/docs/demo/2950-recovery.svg" alt="A real Catalyst 2950 going from the Mode-button bootloader to an unlocked Switch# prompt, replayed from a committed hardware recording" width="100%">
</p>
<p align="center"><sub>Not a mock-up: a real WS-C2950G-24-EI, replayed from
<a href="https://github.com/Ryan-Clinton/ciscoyoke/blob/main/tests/fixtures/hw-switch-2950-recover-no-restore.ytx.pub">its committed recording</a>
with the long silences shortened. Serial numbers and MAC scrubbed.</sub></p>

> **Early alpha.** Run end to end on a real Catalyst 2950; the 2960 family is
> implemented from Cisco's documentation and **wants testers**. See
> [hardware](#hardware-tested-and-wanted).

## Try it

```bash
pipx install ciscoyoke

ciscoyoke doctor          # is the cable and adapter OK?
ciscoyoke scan            # what is on every serial port?
ciscoyoke rescue COM4     # what is this device, and what does it need?
```

**Most people only need `ciscoyoke rescue`.** It identifies the device, works
out its state, preserves whatever it can read, and tells you the safest next
command — then stops, because everything after that changes the device and is
your decision.

## Three real sessions

These are real output from the bench 2950, abridged.

**A switch nobody knows anything about**

```
$ ciscoyoke intake COM4

State:        user_exec (observed/high)
Model:        WS-C2950G-24-EI
IOS version:  12.1(9)EA1
Config reg:   0xF

identified from show version
No destructive action taken.
```

**A locked switch**, with a previous owner's console login and enable secret:

```
$ ciscoyoke recover access COM4 --no-restore --confirm

Platform from memory: WS-C2950G-24-EI (seen on this adapter, from intake)

HUMAN ACTION REQUIRED
  Unplug the switch. Hold the MODE button down, plug the power back in,
  and release it when the STAT LED goes out (about 5 seconds).
  ✓ observed: bootloader

  IOS loads config.text (the default); it will be renamed to config.text.ciscoyoke

Access recovered, with the previous configuration left aside as
flash:config.text.ciscoyoke and not loaded. The device is unconfigured and
at a privileged prompt.
```

**Then a clean baseline**, with everything it deletes read into an archive first:

```
$ ciscoyoke reset COM4 --confirm

  ✓ file_backup.cfg        ✓ file_config.old        ✓ file_config.text.ciscoyoke

  ! delete flash:vlan.dat (destroys the VLAN database)
  ! delete flash:backup.cfg (a previous owner's configuration)
  ! delete flash:config.old (a previous owner's configuration)
  ! delete flash:config.text.ciscoyoke (a previous owner's configuration)
    dir flash: (confirm every deletion)

Reset complete.
```

A fourth, **image rescue** for a device with no bootable IOS (XMODEM transfer,
then proof that IOS actually boots), is implemented but has not met hardware yet.

## Where it fits

ciscoyoke doesn't replace network automation. It gets equipment *into* it.

```
dead / unknown / locked
         │
         ▼
     ciscoyoke           serial console, no IP required
         │
         ▼
known device with an IP
         │
         ├── Netmiko
         ├── Nornir
         ├── Ansible
         └── scrapli     SSH / telnet, IP required
```

Every mainstream tool assumes the device already has an address, a reachable
management interface and credentials that work. A second-hand box has none of
those, and everything between "arrived from eBay" and "automation can reach it"
is usually done by hand with a terminal emulator and a Cisco tech note from 2007.

## Hardware: tested and wanted

Support is claimed only when it's earned, and every mark says how:

```
● Hardware verified      run against a real device; the recording is committed
○ Documentation-derived  implemented from Cisco's published procedure, never run
— Not yet attempted
```

| Device | Identify | Password recovery | Reset | We need |
| --- | --- | --- | --- | --- |
| Catalyst 2950 | ● | ● | ● ¹ | more variants |
| Catalyst 2960 | ○ | ○ | ○ | **a tester** |
| Catalyst 2960-S / X / Plus | ○ | ○ | ○ | **a tester** (USB console too) |
| Catalyst 3550 / 3560 / 3750 | — | — | — | **a tester**, or a profile from Cisco's docs ² |
| Cisco 1700 / 1800 / 1841 routers | — | — | — | **a tester** |

¹ Recorded against a made-up lab configuration; an earlier run that held a
previous owner's configuration was never published.
² No model-specific profile yet: these get Cisco's general procedure with
longer waits, and destructive commands ask for `--accept-unverified`.
Per-mark evidence: [docs/HARDWARE-TESTING.md](https://github.com/Ryan-Clinton/ciscoyoke/blob/main/docs/HARDWARE-TESTING.md).

**Got one of these?** Run `ciscoyoke rescue COM4`. If anything goes wrong:

```
ciscoyoke report     # one zip: the run scrubbed, configuration output removed,
                     # what it expected, what it saw, and your adapter
```

and attach it to a [hardware report](https://github.com/Ryan-Clinton/ciscoyoke/issues/new?template=hardware-report.yml).
That's the most useful contribution there is, and it needs no Python.

## Real sessions become tests

```
real Cisco hardware
       │
       ▼
serial transcript        recorded by every destructive run
       │
       ▼
scrub secrets            passwords, keys, addresses, serials; config output removed
       │
       ▼
fixture committed        tests/fixtures/hw-*.ytx.pub
       │
       ▼
CI replays it forever    on Windows, macOS and Linux, with nothing plugged in
```

309 tests passed before the first real switch was connected. It still found
defects none of them could — LF-CR line endings, a rename that failed silently,
a log message hiding an IOS question — and each now has a test built from what
the real switch sent, most of them replaying its recording directly.

## Designed not to brick your switch

- Destructive commands are **dry runs** until you add `--confirm`.
- Configuration is **read into an archive before anything deletes it**, and the
  archive says plainly what it couldn't read.
- Every change is **journalled before it's sent**, so an interrupted run can be
  reconciled against the device (`ciscoyoke resolve`).
- Renames and deletions are **proven from a fresh flash listing**, not assumed
  from the prompt coming back.
- An image rescue **isn't a success until IOS boots** the image you supplied.
- Recordings stay **private until scrubbed**; CI refuses a raw one.

More: [docs/SAFETY.md](https://github.com/Ryan-Clinton/ciscoyoke/blob/main/docs/SAFETY.md) · [threat model](https://github.com/Ryan-Clinton/ciscoyoke/blob/main/docs/THREAT-MODEL.md) ·
[architecture](https://github.com/Ryan-Clinton/ciscoyoke/blob/main/docs/ARCHITECTURE.md) · [specification](https://github.com/Ryan-Clinton/ciscoyoke/blob/main/docs/SPEC.md)

## Commands

| Look, change nothing | |
| --- | --- |
| `ciscoyoke rescue PORT` | **start here**: identify, preserve, recommend |
| `ciscoyoke scan` | every serial port: state, model, and what each needs next |
| `ciscoyoke doctor` | cable, adapter, permissions and driver checks |
| `ciscoyoke intake PORT` / `health PORT` / `archive PORT` | identify / check / preserve one device |
| `ciscoyoke capture PORT -o F` / `sweep PORT` | record a boot / find the line speed |
| `ciscoyoke report` | package the last run for a bug report |

| Change the device (dry run unless `--confirm`) | |
| --- | --- |
| `ciscoyoke recover access PORT` | password recovery, guided through the Mode button or break |
| `ciscoyoke reset PORT` | erase to a clean lab baseline |
| `ciscoyoke recover image PORT --image F` | XMODEM rescue for a device with no bootable IOS |
| `ciscoyoke lab apply LABFILE` | push per-device lab configuration |

Every option is in [docs/SAFETY.md](https://github.com/Ryan-Clinton/ciscoyoke/blob/main/docs/SAFETY.md) and `ciscoyoke <command> --help`.

## Contributing

You don't need to write Python. Testing on hardware you own, sending a
`ciscoyoke report`, or adding a platform profile from Cisco's documentation are
all real contributions. See [CONTRIBUTING.md](https://github.com/Ryan-Clinton/ciscoyoke/blob/main/CONTRIBUTING.md).

## Firmware

ciscoyoke **never hosts, mirrors, searches for or redistributes Cisco IOS
images**, and no feature accepts a URL to fetch one from. It accepts an image
you supply and automates transport and verification. Lawful entitlement to any
image is your responsibility.

## Licence

MIT.
