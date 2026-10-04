# Developer map

[ARCHITECTURE.md](ARCHITECTURE.md) explains *why* ciscoyoke works the way it
does. This page answers a narrower question: **where do I change X?**

Setup and the one command that says a change is ready are in
[CONTRIBUTING.md](../CONTRIBUTING.md#working-on-the-code).

## Want to change...

| ...this | Look here | Tests |
| --- | --- | --- |
| A command's arguments, help text or output | `cli/`: one module per command group (table below) | `test_cli.py`, `test_front_door.py` |
| What a core command does (`rescue`, `health`, `archive`, `reset`) | `commands.py`, then `lifecycle/` | `test_rescue.py`, `test_archive.py`, `test_clean_reset.py` |
| Which prompt or banner is recognised | `stream/signals.py` | `test_tracker.py`, `test_chunk_invariance.py` |
| How observations become a device state | `stream/tracker.py` | `test_tracker.py` |
| A Catalyst family: Mode-button timing, `load_helper`, console port | `platform_profiles.py` | `test_platform_families.py` |
| Password recovery steps | `playbook/ios_switch.py`, `playbook/ios_router.py`; chosen by `lifecycle/recover.py` | `test_switch_recovery.py`, `test_playbook.py` |
| Steps a person performs (hold Mode, power-cycle) | `playbook/human.py`, driven by `interactive.py` | `test_interactive.py` |
| Reset to an empty baseline | `lifecycle/reset.py` | `test_clean_reset.py`, `test_unconfigured_device.py` |
| Image rescue: planning, compatibility, boot proof | `image/`, orchestrated by `imaging.py` | `test_image.py`, `test_image_drivers.py`, `test_bootproof.py`, `test_console_speed.py` |
| XMODEM or transfer progress | `playbook/xmodem.py`, `playbook/transfer.py` | `test_xmodem.py` |
| Reading model, version, serial from console text | `identify/facts.py`, `identify/probe.py` | `test_intake_replay.py` |
| Serial, USB adapter identity, port labels | `transport/` | `test_transport.py` |
| The expect/send loop, deadlines, pager handling | `session.py` | `test_intake_replay.py`, `test_playbook.py` |
| Passive recording (`capture`, `sweep`, `console`) | `capture.py` (the listener), `capturing.py` (the commands) | `test_capture.py` |
| Transcript format, recording, scrubbing, replay | `transcript/` | `test_transcript.py`, `test_scrub.py`, `test_hardware_fixtures.py` |
| What is journalled, leases, resuming an interrupted run | `journal/`, `support.py` | `test_journal.py`, `test_lease.py` |
| Exit codes, the `--json` result shape | `result/` | `test_cli.py` |
| Lab files, cabling checks, pushing config to a lab | `topology/`, `labs.py` | `test_topology.py` |
| `doctor` host checks | `doctor.py` | `test_cli.py` |
| `report` and `support-bundle` | `report.py`, `support.py`, `diagnose.py` | `test_remote_support.py` |
| Terminal output that survives a legacy console | `ui.py` | `test_ui.py` |
| Secrets and how they are kept out of logs | `credentials.py`, `transcript/recorder.py` | `test_credentials.py` |

All paths are under `src/ciscoyoke/`; all tests are under `tests/`.

Four packages have a short README of their own with the invariants a change
must keep: [`stream/`](../src/ciscoyoke/stream/README.md),
[`playbook/`](../src/ciscoyoke/playbook/README.md),
[`transcript/`](../src/ciscoyoke/transcript/README.md) and
[`image/`](../src/ciscoyoke/image/README.md).

## Where each command lives

Every command has two halves: its arguments and output in `cli/`, and the work
itself in a `do_*()` function.

| Command | Arguments and output | The work |
| --- | --- | --- |
| `doctor` | `cli/doctor.py` | `doctor.py` |
| `ports`, `ports label`, `ports labels` | `cli/ports.py` | `transport/identity.py`, `transport/labels.py` |
| `scan`, `intake`, `login` | `cli/identify.py` | `identify/probe.py`, `interactive.py` |
| `rescue`, `health`, `archive`, `reset` | `cli/recovery.py` | `commands.py` |
| `recover access`, `resolve` | `cli/recovery.py` | `interactive.py`, `support.py` |
| `recover image` | `cli/image.py` | `imaging.py` |
| `lab verify`, `lab apply` | `cli/lab.py` | `labs.py` |
| `transcript scrub`, `transcript replay` | `cli/transcript.py` | `transcript/scrub.py`, `transcript/replay.py` |
| `console`, `capture`, `sweep` | `cli/capture.py` | `capturing.py` |
| `support-bundle`, `report` | `cli/support.py` | `support.py`, `report.py` |

`cli/parser.py` assembles them, in the order `ciscoyoke --help` lists them.
`cli/common.py` has what they share: printing a result, turning a failure into
its exit code, and resolving a port label.

To add a command: put a `cmd_*` handler and its arguments in the module for its
group, register it there, and write the work as a `do_*()` that can be called
and tested without argparse.

## How the layers depend on each other

```text
cli/                       argparse only: turn a command line into one call
        │
        ▼
commands.py, imaging.py,   one do_*() per command: take the lease, open the
interactive.py, labs.py,   journal, call the layers below, return text + exit code
capturing.py, support.py
        │
        ├── identify/      what is this device?
        ├── lifecycle/     rescue, health, archive, reset, access recovery
        ├── image/         image rescue
        └── topology/      lab files and cabling
                │
                ▼
          playbook/        declared, guarded steps ──► journal/ (every mutation)
                │
                ▼
          session.py       expect/send ──► stream/ (bytes → observed state)
                │
                ▼
          transport/       serial, RFC 2217, sockets
                │
                ▼
             device
```

Dependencies point down. `stream/`, `transport/`, `result/` and `transcript/`
import nothing above them, which is what lets the test suite replace the device
with a recording (`transcript/fake.py`) and leave everything else real.

## Locality of change

A design goal, and a question worth asking of any change: *how many unrelated
files does someone have to understand to make it?*

Adding a Catalyst family should touch:

1. `platform_profiles.py`: one profile entry, with the Cisco document it came
   from.
2. `tests/test_platform_families.py`: what the profile claims, and what it
   must not claim (a prefix that also matches a different family is the usual
   mistake).
3. `docs/HARDWARE-TESTING.md` and the README table: the mark it has earned.
4. A playbook or image driver only if the procedure really differs.

It should not need `cli/`, `commands.py`, `journal/`, `result/` or
`transport/`. If a change like this drags one of those in, say so in the pull
request: that is a design problem worth fixing, not something to work around.

Likewise a newly recognised prompt is one pattern in `stream/signals.py` and one
test built from the real text.

## Where this is heading

The tree is being regrouped so one concept has one obvious home. It happens a
small pull request at a time, never as one large move:

- Done: `cli.py` + `cli_extra.py` became the `cli/` package.
- `imaging.py` moves into `image/`.
- `capture.py` + `capturing.py` become a `capture/` package.
- Later: `lifecycle/` and the top-level command modules settle into clearer
  neighbourhoods as they are next touched.

This page is updated in the same pull request as each move.
