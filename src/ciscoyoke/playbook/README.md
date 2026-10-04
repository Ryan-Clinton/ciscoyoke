# playbook

Device procedures written as data: declared, guarded steps that can be checked
before a byte is sent.

| File | What it holds |
| --- | --- |
| `base.py` | `Step`, `plan()` and `execute()`. A step declares the states it may start from, what it sends, the states it expects, the capabilities it needs and what it destroys. |
| `ios_switch.py` | Access recovery for fixed-configuration Catalysts (Mode button, `switch:` bootloader). |
| `ios_router.py` | Access recovery for IOS routers (break, ROMMON, config register). |
| `human.py` | Physical actions a person performs, modelled as steps that are verified from the console. |
| `transfer.py`, `xmodem.py` | Moving an image onto a device, with progress. |

**Entry points:** `plan(steps, ...)` then `execute(...)` in `base.py`. Which
playbook runs is chosen in `lifecycle/recover.py`; per-family details come from
`platform_profiles.py`.

## Invariants

- **Every step is guarded.** None may fire into a state it did not declare.
- **Refuse at plan time.** A missing transport capability or a device that has
  announced it will destroy its configuration stops the plan while the device
  is still untouched, never halfway through.
- **A destructive step carries its mutation**, so it is journalled before it
  runs and can be resumed or rolled back.
- **The prompt coming back proves nothing.** A bootloader answers a failed
  `rename` with the same `switch:` as a successful one. Renames and deletions
  are proven from a fresh flash listing.
- **Catalyst and router recovery are separate playbooks**, not one with
  branches.

## Tests

`tests/test_playbook.py`, `tests/test_switch_recovery.py`,
`tests/test_interactive.py`, `tests/test_xmodem.py`.

## Common change: a family that differs only in details

That is a profile, not a playbook: add an entry to `platform_profiles.py`. Edit
a playbook only when the procedure itself differs, and add a test that plans it
against a recorded or documented session.
