# Changelog

All notable changes. Versions follow [PEP 440](https://peps.python.org/pep-0440/);
the format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- A fourth hardware recording: `reset` erasing a lab-only configuration on the
  WS-C2950G-24-EI, which earns the 2950 its reset mark.
- A documentation-derived profile for the Catalyst 3560, 3560G and 3560V2,
  from Cisco's recovery guide. The 3560-X and 3560-CX stay on the generic
  procedure: the CX releases Mode on a different LED cue. (#11, @soyeladice-svg)
- A device stuck in a power-on self-test failure loop is now recognised as a
  hardware fault: `intake` and `scan` say so, and `rescue` recommends stopping
  rather than checking the cable. A live prompt always outranks earlier
  failure text. (#12, @soyeladice-svg)
- Docs: which USB console cables work, and which to avoid. (#10, @soyeladice-svg)

### Fixed (found on the bench 2950 while recording that reset)
- The scrubber left the chassis MAC in `snmp-server engineID local ...`, where
  it appears without separators.
- The scrubber replaced netmasks and wildcards as though they were addresses.
- `reset` refused an unconfigured device waiting at the setup dialog or at
  `Switch>`. It now declines the dialog and enables first, and still refuses
  if the device asks for a password.
- `rescue` had no next step for a device at the setup dialog; it now says the
  device is usable and recommends `reset` to clear what flash may still hold.
- Refusals printed with garbled dashes on a legacy Windows console.

## [0.1.0a1] - 2026-09-30

First public alpha. Run end to end on a real Catalyst 2950; the 2960 family is
implemented from Cisco's documentation and wants testers.

### Added
- `rescue`, `scan`, `intake`, `health`, `archive`, `doctor`, `capture`,
  `sweep`: identify and preserve without changing anything. `scan` shows what
  each device needs next.
- `recover access`: guided password recovery for Catalysts (Mode button) and
  routers (break), with `--no-restore` for a clean device and `--platform` for
  a locked one.
- `reset`: a clean lab baseline, deleting `vlan.dat` and stale configuration
  files only after archiving them, and proving the deletions.
- `recover image`: XMODEM image rescue ending in boot proof.
- Platform profiles per Catalyst family, each labelled by the evidence behind
  it (`hardware`, `documentation`, `generic`).
- `report` and `transcript replay`: a failed run on hardware nobody here owns
  becomes a scrubbed report, then a replayed fixture and a failing test.
- Three scrubbed recordings from a real WS-C2950G-24-EI as test fixtures.

### Fixed (found on first contact with real hardware)
- Bootloader prompts ending LF-CR were not recognised.
- A rename over an existing `config.old` failed silently, and the switch booted
  back into its login.
- A console log message landing after an IOS question hid the question.
- Scrubbing missed secrets split across serial reads, a RADIUS key written
  after `auth-port`/`acct-port`, banners naming the previous owner, and serial
  numbers and MAC addresses.

[Unreleased]: https://github.com/Ryan-Clinton/ciscoyoke/compare/v0.1.0a1...HEAD
[0.1.0a1]: https://github.com/Ryan-Clinton/ciscoyoke/releases/tag/v0.1.0a1
