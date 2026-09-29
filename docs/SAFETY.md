# Safety: what the destructive commands do, exactly

Everything here changes a device. Every one of them is a **dry run until you add
`--confirm`**, takes a lease on the console so a second terminal can't
interleave with it, journals each change before sending it, and records the
whole session to a private transcript under the state directory
(`%LOCALAPPDATA%\ciscoyoke\transcripts` on Windows, `~/.local/state/ciscoyoke`
on Linux).

## `ciscoyoke recover access PORT`

Password recovery, platform-aware.

- **Catalyst:** guides you through the Mode button, with the release timing for
  your switch family, and confirms from the console that the bootloader was
  reached. It then reads `CONFIG_FILE` and the flash listing, renames whichever
  file IOS actually loads (never over an existing file), proves the rename from
  a fresh listing, boots without a configuration, answers the setup dialog (and
  IOS 15's autoinstall question), and enters privileged mode.
- **Router:** drives the break window and the configuration register.
- By default it then puts the old configuration back and loads it into the
  running configuration, so you can set a new password and keep everything
  else.
- `--no-restore` stops at the unlocked prompt with the old configuration still
  aside, for when you want a clean device: follow it with `reset`, which
  archives the moved file and deletes it.
- The platform comes from the device, else from the model last seen booting on
  the same adapter, else `--platform router|catalyst`. A stated platform the
  device contradicts is refused.
- A Catalyst family with no profile needs `--accept-unverified`.

## `ciscoyoke reset PORT`

Erase to a known-empty lab baseline.

- `write erase`, then on a Catalyst: `vlan.dat` (without it the switch comes
  back with the previous owner's VLANs), any configuration left in flash under
  another name (`config.old`, `*.cfg`, a moved-aside `config.text.ciscoyoke`),
  and a non-default `boot config-file`.
- Every file it's about to delete is read into the archive first. What couldn't
  be read is listed before you confirm.
- The deletions are proven from a fresh flash listing before the reload.
- `--archive-to DIR` writes the archive somewhere you choose. Archives are
  private: they hold somebody's configuration.
- Reset is **not sanitisation**. It makes no claim about residual data in flash
  or NVRAM.

## `ciscoyoke recover image PORT --image FILE`

For a device with no bootable IOS: transfers an image you supply over XMODEM,
optionally raising the console speed for the transfer (journalled, so an
interruption can be reconciled at the speed actually in use), and doesn't report
success until IOS has booted the expected image. `--via tftp` refuses with an
explanation: the provider exists, but the command doesn't yet collect the
addressing a ROMMON TFTP boot needs, and a half-configured TFTP boot strands a
device.

## `ciscoyoke lab apply LABFILE`

Pushes per-device configuration to a lab, matched to devices by adapter
identity. Devices matched only by port name are refused unless you pass
`--allow-weak-match`.

## When a run is interrupted or fails

A change that was sent but never observed blocks the next operation on that
device until `ciscoyoke resolve PORT` has looked at the device and settled it:
baud changes by probing both speeds, flash renames and deletions by listing
flash. `--rollback` undoes what can be undone and says what can't.

A failed run also writes `<run>.failure.json` beside its transcript: the failed
step, what it expected and observed, the last unrecognised prompt, the state
history, and the adapter. `ciscoyoke report` packages it for a bug report.

See also: [THREAT-MODEL.md](THREAT-MODEL.md) for what can go wrong and which
invariant prevents it.
