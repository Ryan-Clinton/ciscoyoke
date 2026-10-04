# image

Getting a user-supplied IOS image onto a device that cannot boot, and proving
it came back.

| File | What it holds |
| --- | --- |
| `fingerprint.py` | What the supplied file is: size, hash, and what its name claims. |
| `compat.py` | Whether the image plausibly suits the device, and how sure that is. |
| `plan.py` | Flash-space arithmetic and the stranding rule. |
| `drivers.py` | The per-platform commands: Catalyst bootloader versus router ROMMON. |
| `tftp.py` | The embedded TFTP server, bound as narrowly as the job allows. |
| `bootproof.py` | Proof that the device booted the image that was sent. |

**Entry point:** `do_recover_image()` in `../imaging.py` runs the whole
`recover image` command using the modules here. The transfer itself is in
`../playbook/transfer.py` and `../playbook/xmodem.py`.

## Invariants

- **Check everything before sending anything.** Fingerprint, compatibility,
  flash inventory and the stranding rule all run first.
- **The only known-bootable image is never deleted to make room** without a
  separate, explicit acknowledgement (`--accept-stranded-risk`).
- **A transfer is not a rescue until the device boots**, and boots *that* image:
  the same version string from a different file is not a pass.
- **The console speed change is journalled.** An interrupted transfer leaves
  the device at 115200 and the journal is the only record of that.
- **The TFTP server binds one chosen interface**, never `0.0.0.0`, and shuts
  down when the job ends.

## Tests

`tests/test_image.py`, `tests/test_xmodem.py`.

## Common change: a platform with different bootloader commands

Add or extend a driver in `drivers.py` and a test that the right driver is
chosen for the device state. Nothing in `plan.py` or `bootproof.py` should need
to know which platform it is.
