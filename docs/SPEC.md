# ciscoyoke — specification

**A rescue bench for old Cisco hardware.**
Discover, diagnose, preserve, recover and commission Cisco gear from the serial
console — no management IP required.

> **North star.** Take an unknown Cisco box from "pulled from a rack / bought
> second-hand" to a known-good, documented lab node — from the serial console,
> with no management IP required.

Status: draft specification, revision 4. Nothing implemented.

**This is the last spec revision before code.** Revision 4 closes the remaining
contradictions and hardens the foundations; it deliberately adds no new
features. Further questions get answered by M0/M1 and real Cisco boxes, not by
another hundred lines of design document.

---

## 1. Four commitments

Everything below is an expression of these. Where a design choice is unclear,
these decide it.

**1. Evidence, not guesses.** Every identity, state, capability and
compatibility conclusion knows *why* it believes something, and says so. The
tool distinguishes what it observed from what it inferred from what it does not
know.

**2. Preserve what is possible; disclose what is not.** Preservation is
partial by nature — you cannot read a configuration you cannot authenticate to.
The tool never implies perfect preservation, and explicitly records what it
could not save and what continuing would cost.

**3. Recoveries are restartable.** A dead cable, a sleeping laptop or a Python
crash must not leave ciscoyoke confused about what it changed. Every mutation
declares its recovery semantics before it is attempted, and every operation has
re-entry semantics. Not every mutation is reversible — `write erase` is not —
so the commitment is that irreversibility is *declared and guarded*, never
discovered afterwards.

**4. Enforceable safety properties are machine-testable invariants, not
documentation warnings.** Every runtime safety guarantee ciscoyoke claims is
backed by an invariant or an explicit verification test (§12.2). Residual risks
it cannot prove — lawful firmware entitlement, whether a scrubbed transcript
still contains an unrecognised secret, whether a host firewall exposes more than
expected — are **stated rather than implied away**.

---

## 2. The problem

Every network automation tool in common use — netmiko, scrapli, Nornir, NAPALM,
Ansible — connects over SSH or telnet, and assumes the device already has an IP
address, a reachable management interface, and credentials that work.

A second-hand Cisco device has none of those. It arrives with a stranger's
configuration, an unknown enable password, an unverified IOS version, possibly
no working boot image at all, and no IP address. Everything between "box arrives
from eBay" and "normal automation can reach it" is done by hand: a console
cable, a terminal emulator, and a Cisco tech note from 2007 in another window.

### 2.1 The lifecycle

```
Discover → Intake → Preserve → Recover access → Recover image → Reset → Prove → Hand off
```

| Stage | What it means |
| --- | --- |
| **Discover** | Enumerate adapters and console endpoints without assuming a reachable device |
| **Intake** | Passive-first state detection, boot capture, identity and facts — each with a confidence |
| **Preserve** | Build an evidence bundle *before* anything destructive, recording what could not be captured |
| **Recover access** | Automate what can be automated; guide physical Mode-button and power actions when physics requires a person |
| **Recover image** | Boot an existing valid image, or transfer a user-supplied one and prove it booted |
| **Reset** | Create a known lab baseline — explicitly not a sanitisation claim |
| **Prove** | Health checks and physical cabling verification |
| **Hand off** | Emit inventory that netmiko, Nornir and Ansible can consume |

`ciscoyoke rescue` (§14.1) runs this sequence as one command. The individual
verbs remain available for people who want them.

### 2.2 The pain map

| Pain | What the user faces | Response | Priority |
| --- | --- | --- | --- |
| Unknown state | No IP, unknown baud, unknown prompt, possibly mid-boot | Passive-first scan with evidence and confidence | P0 |
| Locked config | Unknown login/enable; platform-specific recovery | Access-recovery playbooks with human steps | P0 |
| Previous-owner data | Configs, keys, addressing and credentials remain on the device | Archive first, with explicit disclosure of what could not be read | P0 |
| Interrupted recovery | Cable pulled mid-transfer; device left at a changed baud | Recovery journal and state convergence | P0 |
| Missing/corrupt IOS | Stranded at `rommon` or `switch:` with no valid image | Image rescue with flash planning and rollback protection | P1 — hero |
| Repeated lab teardown | Manual erase, reload and dialog handling, per device | Reset and verify | P1 |
| Serial weirdness | COM churn, driver quirks, wrong baud, inconsistent break support | `doctor`, `ports`, stable adapter labels | P0 |
| Cabling drift | Physical patching no longer matches the intended topology | CDP declared-versus-observed diff | P2 — demo |

### 2.3 Where this sits

```
   eBay box  ──►  [ ciscoyoke ]  ──►  device has an IP  ──►  [ netmiko, Nornir, Ansible ]
                  console/serial                              SSH / telnet
                  no IP required                              IP required
```

### 2.4 The novelty claim, stated narrowly

We found no obvious open-source project focused on **combining** local serial
discovery, old-Cisco intake, preservation, guided physical recovery, image
rescue, lab reset and physical topology verification behind a low-friction CLI.

Deliberately narrower than "nobody automates Cisco recovery", which is false.
See §3.

### 2.5 Is there an audience?

A niche, with a specific motivation. Current CCNA and home-lab discussions
contain people deliberately buying older Cisco hardware because they want to
cable it, break it, recover it and see behaviour a simulator abstracts away —
including people who find an old 2960 and immediately want to reset it and start
labbing. The counter-pressure is real: noise, power draw and inconvenience push
most people to Packet Tracer and CML.

The design consequence: **remove the tedious parts of owning old hardware
without erasing the physical experience those people bought it for.**

---

## 3. Competitive position

| Adjacent tool | What it genuinely owns | ciscoyoke differentiation |
| --- | --- | --- |
| **netmiko** | Mature multi-vendor SSH/telnet, plus a `cisco_ios_serial` driver | Lifecycle, boot and recovery states, preservation and evidence — not transport alone |
| **ConsolePi** | A capable serial console server: aliases, remote access, baud control around ser2net | Drives the device *after* the console is reached; consumes ConsolePi rather than replacing it |
| **ser2net** | Serial-to-network transport including RFC2217 | A state, recovery and application layer above transport |
| **pyATS / Genie Clean** | Enterprise testbed and clean workflows that **do** include ROMMON recovery (`rommon_boot`, 22.4), Device Recovery, and password recovery in the connect stage (24.8) | Local USB and old/EOL-first UX; low setup cost; offline replay corpus; preservation; guided physical actions |
| **Packet Tracer / CML** | Virtual practice and repeatable topologies | Real hardware pathology: POST, flash, bootloader, serial, cables, power, recovery |

The pyATS row matters. Genie Clean assumes a testbed YAML, a managed
environment and typically network reachability or a power controller.
**The niche is not "recovery is unautomated" — it is "recovery is unautomated
for a person with one cable and no infrastructure."**

---

## 4. Non-goals

- **Not an SSH automation framework.** Once a device has an IP and credentials,
  ciscoyoke's job ends and netmiko's begins.
- **Not a console server.** ConsolePi and ser2net own that; ciscoyoke speaks to
  them.
- **Not a lab grader.** No marking, no scoring, no exercise content.
- **Not a simulator.** It drives real hardware.
- **Not multi-vendor.** Cisco IOS and its bootloaders. Depth over breadth.
- **Not a sanitisation product.** It archives and resets; it does not claim
  verified data destruction (§10.2).
- **Never a firmware distributor.** It never hosts, mirrors, searches for or
  redistributes IOS images (§11.5).

---

## 5. The hard problem

A serial console has no connection event, no handshake, no protocol. You join a
byte stream already in progress. The device may be mid-POST, at a login prompt,
in ROMMON, holding a pager, or silent. There is no query that returns its state.

### 5.1 Byte-boundary correctness

Serial data arrives in arbitrary chunks. A prompt can straddle two reads —
`Router` in one, `#` in the next — and a detector that regexes each read in
isolation misses it, then hangs until timeout. The most common defect in
hand-rolled console scripts. §12.2 specifies the property test that forbids it.

### 5.2 Signals are not states

A regex match is evidence, not a conclusion. `Password:` may be a login prompt,
an enable prompt, or a line inside a config someone is paging through. The same
bytes mean different things depending on what was sent immediately before.

- **Signals** — pure, context-free detectors. No interpretation, no history, no
  I/O.
- **StateTracker** — accumulates signals into a contextual conclusion, weighing
  the prior command, the expected transition, quiescence and prompt anchoring.
  Emits a state **and a confidence**.

### 5.3 The state graph

| State | Detected by | Escape |
| --- | --- | --- |
| `UNKNOWN` | nothing conclusive yet | send `\r`, re-read |
| `SILENT` | no bytes within the wake timeout | send `\r`; if still silent, `doctor` the port |
| `BOOTING` | POST / self-decompression output flowing | wait, with a boot deadline |
| `ROMMON` | `rommon \d+ >` | `boot`, or `confreg` / `reset` |
| `BOOTLOADER` | `switch:` | `boot`, or `flash_init` |
| `SETUP_DIALOG` | `Would you like to enter the initial configuration dialog? [yes/no]:` | `no\r` |
| `PRESS_RETURN` | `Press RETURN to get started` | `\r` |
| `LOGIN_USERNAME` | `Username:` | supply credential |
| `LOGIN_PASSWORD` | `Password:` in a login context | supply credential, else access recovery |
| `ENABLE_PASSWORD` | `Password:` following an `enable` | supply credential, else access recovery |
| `USER_EXEC` | `\S+>` | `enable` |
| `PRIV_EXEC` | `\S+#` | terminal state for most operations |
| `CONFIG_MODE` | `\S+\(config[^)]*\)#` | `end` |
| `PAGER` | `--More--` | space, or `terminal length 0` |
| `TRANSFER` | XMODEM/TFTP progress or protocol chatter | the transfer state machine owns the stream (§11) |
| `HUMAN_ACTION` | waiting on a person | watch for expected evidence (§8) |
| `DESTRUCTIVE_RECOVERY_GUARD` | platform-specific recovery-disabled prompt | refuse and abort (§10.3) |

The two `Password:` states are distinguished by what was sent before them — the
reason signals and states are separate layers.

---

## 6. Architecture

```
ciscoyoke/
├── transport/
│   ├── base.py         Transport interface + capability model (§7.1)
│   ├── serial_.py      local USB/serial via pyserial
│   ├── rfc2217.py      ser2net / ConsolePi endpoints (§7.2)
│   └── identity.py     USB fingerprinting and stable labels (§7.3)
├── stream/
│   ├── buffer.py       byte-boundary-safe buffering
│   ├── signals.py      pure detectors — prompts, banners, errors, pager, transfer
│   └── tracker.py      StateTracker: transition graph, history, confidence
├── session.py          I/O, deadlines, wake behaviour, pager handling, transcript events
├── journal/
│   ├── model.py        Mutation: reversibility class, compensation, effects (§9.1)
│   ├── store.py        write-ahead journal on WAL-mode SQLite (§9.2)
│   ├── paths.py        state directory via platformdirs (§9.3)
│   ├── lease.py        one owner per transport identity (§9.5)
│   └── converge.py     resume: probe, reconcile, continue or compensate
├── credentials.py      getpass / --password-stdin; never journalled or logged (§10.4)
├── playbook/
│   ├── base.py         declarative guarded steps + destructive-effect metadata
│   ├── human.py        HumanStep — instruction + machine-observable evidence (§8)
│   ├── transfer.py     TransferStep — XMODEM/TFTP progress, console-setting management
│   ├── ios_router.py   1700/1800/2600/2800 access + image playbooks
│   └── ios_switch.py   2950/2960/3550/3560 access + image playbooks
├── identify/
│   ├── probe.py        passive-first; wake only when necessary; changes nothing
│   └── facts.py        model / IOS / flash / DRAM / confreg / boot vars, each with confidence
├── lifecycle/
│   ├── rescue.py       the orchestrator (§14.1)
│   ├── archive.py      evidence bundle + manifest + disclosure of gaps (§10.1)
│   ├── recover.py      access recovery and image rescue
│   ├── reset.py        known-empty lab baseline
│   └── health.py       POST, flash, memory, environment checks
├── image/
│   ├── fingerprint.py  SHA-256 and provenance
│   ├── compat.py       compatibility evidence and confidence (§11.2)
│   ├── plan.py         flash-space planning and rollback protection (§11.3)
│   └── tftp.py         TftpProvider: embedded (privilege-gated) or external (§11.4)
├── topology/
│   ├── labfile.py      lab file schema and validation (§13)
│   ├── apply.py        push config to N devices concurrently
│   └── verify.py       CDP declared-versus-observed diff
├── transcript/
│   ├── schema.py       versioned record format
│   ├── recorder.py     secret-aware recording (§10.5)
│   ├── scrub.py        deterministic raw → shareable, TX and RX
│   └── fake.py         replay a transcript as a device — the test spine (§12)
├── result/
│   ├── schema.py       versioned machine-readable results (§14.4)
│   └── exits.py        documented exit codes (§14.5)
├── doctor.py           host serial/driver/permission/capability diagnostics
└── cli.py
```

**Dependencies:** `pyserial` (transport, including `socket://` and `rfc2217://`),
`tftpy` (constrained TFTP server, MIT, actively released), `platformdirs` (OS
state directory), `textfsm` + `ntc-templates` (parsing), `pydantic` (schemas),
`rich` (terminal output). Journal storage is `sqlite3` from the standard
library.

Deliberately not netmiko: its serial driver assumes an already-booted,
already-authenticated device — precisely the assumption this tool removes.

---

## 7. Transport

### 7.1 Capabilities are tri-state, not boolean

`send_break=True` conflates three different claims. pySerial exposing
`send_break()` does not prove a £7 adapter generates a break, and an adapter
generating a break does not prove *this device* recognised it.

```python
Capability(
    name="send_break",
    api_support=SUPPORTED,       # the library exposes it
    adapter_support=INFERRED,    # chipset is known to implement it
    device_verified=False,       # no device has actually responded to one yet
)
```

Rendered:

```
BREAK
  API support          yes
  adapter support      inferred (FTDI FT232R)
  device verification  not tested
```

and after a successful ROMMON entry:

```
BREAK
  device verification  verified on CISCO1760 (2026-09-05)
```

Verification is **earned and cached against the adapter and device**, so the
tool gets more confident with use rather than asserting up front. Playbook
requirements are checked before execution: an impossible recovery path fails at
plan time, while the device is still untouched, never halfway through.

### 7.2 Remote transport — real, and not free

Supporting `socket://` and `rfc2217://` gets ConsolePi and ser2net users into
the same state engine. It is worth doing at P1. It is not free, and the
capability model is exactly how the differences get expressed:

| Endpoint | Read/write | Change baud | Break | Security |
| --- | --- | --- | --- | --- |
| `serial://COM3` | yes | yes | yes | local |
| `socket://consolepi:7001` | yes | **no** | **no** | **plaintext** |
| `rfc2217://consolepi:7001` | yes | yes | yes | **plaintext** |

pySerial's documentation is explicit on both points: for `socket://`, *"All
serial port settings, control and status lines are ignored. Only data is
transmitted and received"*; and for both handlers, *"The connection is not
encrypted and no authentication is supported! Only use it in trusted
environments."*

Consequences that must be surfaced, not buried:

- A `socket://` endpoint **cannot** perform break-based ROMMON entry or the
  baud-drop fallback. Any recovery playbook needing them fails at plan time with
  a message naming the missing capability.
- Both remote transports are plaintext. ciscoyoke warns when a recovery that
  transmits credentials is attempted over one.
- Network round-trips add latency to an already timing-sensitive protocol.
  pySerial's docs do not warn about this specifically — they expose a `timeout=`
  knob for slow servers — but break timing and XMODEM pacing are sensitive
  enough that remote transports carry longer default deadlines.

### 7.3 Adapter identity and stable labels

COM numbering shuffles between reboots; `/dev/ttyUSB*` follows plug order. This
is the daily indignity of a multi-device bench, and it is also the direct cause
of threat T1 — resetting the wrong device.

pySerial exposes USB metadata where the OS provides it: VID, PID, serial number
and physical location. ciscoyoke uses it to give ports a stable identity and a
human name:

```
$ ciscoyoke ports

PORT   ADAPTER              USB SERIAL   LOCATION   LABEL
COM3   FTDI FT232R          A10K8Z4P     1-4.2      bench-left
COM5   Prolific PL2303      unknown      1-4.3      bench-right
COM7   FTDI FT232R          A10K8Z51     1-4.4      —

$ ciscoyoke ports label COM3 bench-left
$ ciscoyoke rescue bench-left
```

Labels bind to USB serial number where available, physical location otherwise —
and the difference is reported, because a Prolific adapter with no serial number
cannot be told apart from its twin. Small feature, large quality-of-life gain,
and it restores the adapter fingerprinting that revision 1 had and revision 2
lost.

---

## 8. Recovery models physical humans

Catalyst fixed-configuration password recovery requires holding the Mode button
while reconnecting power. No software makes that automatic. A tool that hides it
behind an `unlock` verb is lying about physics.

```
Step 3/8   HUMAN ACTION REQUIRED

  Hold the MODE button on WS-C2950-24 while reconnecting power.
  Keep holding until the console shows the bootloader prompt.

  Why: the bootloader is only reachable through this sequence on this platform.

  Watching console...
  ✓ bootloader prompt observed: switch:

  Continuing automatically.
```

`HumanStep` carries the instruction, the reason, the **machine-observable
evidence** proving it was done, a timeout, cancel/abort semantics, and a
recovery hint if the wrong state appears. The tool never takes the user's word —
it watches the stream for proof. The same abstraction covers manual power
cycling and cable moves.

---

## 9. Re-entrancy: the recovery journal

**The environment this tool claims to handle is exactly the environment where
operations get interrupted.**

Consider image rescue. To make a two-hour XMODEM transfer bearable, the console
is switched to 115200 baud. Then the laptop sleeps, or the USB cable is nudged
out, or Python crashes, or the user hits Ctrl-C. The device is now sitting at a
baud rate ciscoyoke chose, mid-recovery, and the next run has no idea.

A linear playbook cannot handle this. Playbooks are therefore
**state-convergent and restartable**.

### 9.1 Mutations declare recovery semantics

Revision 3 said every mutation is journalled *with a compensation*. That is not
achievable and was the spec's most important remaining contradiction: there is
no compensation for `write erase`, for deleting the last image, or for accepting
config loss. Pretending otherwise weakens the safety model by making the
strongest-sounding guarantee the least true one.

Every mutation instead declares its recovery semantics explicitly:

```python
Mutation(
    action=...,
    reversibility=REVERSIBLE | COMPENSATABLE | IRREVERSIBLE,
    compensation=None | Action,
    destructive_effects=[...],
    preconditions=[...],
    postconditions=[...],
)
```

| Mutation | Class | Compensation |
| --- | --- | --- |
| console baud 9600 → 115200 | `COMPENSATABLE` | restore 9600 |
| config register 0x2102 → 0x2142 | `COMPENSATABLE` | restore 0x2102 |
| `rename flash:config.text` | `REVERSIBLE` | rename back |
| `write erase` | `IRREVERSIBLE` | none — startup-config destroyed |
| delete the only IOS image | `IRREVERSIBLE` | none — rollback image destroyed |

The rule, which is much harder to talk your way around than the old one:

> **Every mutation has explicitly declared recovery semantics. Reversible and
> compensatable mutations must carry a compensation. Irreversible mutations must
> declare that no compensation exists and pass their associated safety guard
> before execution.**

### 9.2 Write-ahead journal semantics

"Updated before each mutation" leaves a real ambiguity. Journal first and crash
before sending, and the journal claims something that never happened. Send
first and crash before journalling, and the device changed with no record.

With an external physical device you cannot eliminate that uncertainty — so
model it rather than hide it. Every mutation moves through a lifecycle:

```
PREPARED → ATTEMPTED → OBSERVED_APPLIED → POSTCONDITION_VERIFIED → COMMITTED
```

```yaml
mutation_id:    17
type:           console_baud
from:           9600
to:             115200
reversibility:  compensatable
status:         attempted
before:
  baud:     9600
  evidence: console_response
expected_after:
  baud:     115200
compensation:
  set_baud: 9600
```

**After an interruption, an `attempted` entry is never trusted.** It is a
question, not a fact, and the answer comes from probing the device. The formal
rule:

> **Write intent durably, perform the action, observe reality, then commit the
> result. After interruption, observed reality always resolves incomplete
> journal entries.**

**Storage is SQLite**, not hand-rolled YAML or JSON. Transactions, crash-safe
commits and WAL are exactly the semantics required here, and inventing them by
hand is how state files end up truncated by the crash they exist to survive.

### 9.3 Journal location

State lives in the OS user state directory, resolved with `platformdirs` —
**not** alongside transcripts. They are different things: the journal is
operational truth needed to resume safely; a transcript is evidence and history.

```
~/.local/state/ciscoyoke/          (Linux)
%LOCALAPPDATA%\ciscoyoke\          (Windows)
├── state.db                       journal, WAL-mode SQLite
└── locks/                         transport leases (§9.5)
```

`ciscoyoke support-bundle` exports the relevant journal alongside the scrubbed
transcript. Moving a recovery between machines (`journal export` / `import`) is
a much later concern; the default stays safe and simple.

### 9.4 Resume

```
$ ciscoyoke rescue bench-left

Previous interrupted recovery detected (2026-09-05 20:14, recover_image).

Last journal entry:
  mutation 17  console_baud 9600 → 115200  status: ATTEMPTED (unverified)

Probing...
  ✓ no response at 9600
  ✓ device found at 115200        → mutation 17 resolved: OBSERVED_APPLIED
  ✓ state: switch:
  ✓ flash image incomplete (partial transfer, 1.9 MiB of 2.9 MiB)

Recovery can safely resume.
Resume, or roll back to 9600 and abort? [resume/rollback]
```

On start, ciscoyoke reconciles the journal against observed reality: it probes
at the *journalled* baud as well as the default, resolves every incomplete entry
by observation, and either resumes from the last verified postcondition or
compensates back to the starting state.

Where reality contradicts the journal — the device is at 9600 after all, because
someone power-cycled it — **observed reality wins and the journal is corrected.
The journal is a hypothesis, not an authority.** That is commitment 1 applied to
the tool's own history.

### 9.5 One owner per transport

Two ciscoyoke processes on the same device is a corruption scenario: two writers
interleaving on one console, two journals disagreeing, a forgotten background
process still holding the port.

A **lease** is taken before any stateful operation, and it is keyed on the
**transport identity** — the USB serial number or physical location from §7.3 —
not on the port name. Two labels resolving to the same adapter therefore collide
correctly, which port-name locking would miss.

```
$ ciscoyoke rescue bench-left

ERROR: target is already in use

  PID       18442
  Command   recover image
  Started   20:31:12
  Journal   7f3a...
  Held by   USB A10K8Z4P (COM3)

Exit code 70.
```

Leases are held in `locks/` beside the journal, carry the owning PID, and are
reclaimed automatically when that process no longer exists — a crash must not
require manual unlocking.

> **Invariant: at most one mutating ciscoyoke session owns a physical transport
> path at a time.**

### 9.6 Why this matters

Ordinary production infrastructure thinking — failure is expected, therefore
every operation needs re-entry semantics — applied somewhere it almost never is.
For a reviewer it is the clearest signal in the repository that the author has
operated software, not only written it.

---

## 10. Safety, preservation and privacy

### 10.1 Archive preserves what it can and discloses what it cannot

Revision 2 claimed archive meant "nothing is lost". That is not achievable. You
cannot read a startup-config you cannot authenticate to. The honest claim:

> **Archive captures everything safely observable before mutation, and
> explicitly records everything it could not preserve.**

```
Preservation status: PARTIAL

  ✓ boot transcript
  ✓ observed platform evidence
  ✓ bootloader variables
  ✓ flash directory listing
  ? running-config       inaccessible without credentials
  ? startup-config       inaccessible without credentials
  ? private-config       accessibility unknown
  ? cryptographic keys   accessibility unknown

Destructive recovery may make the unavailable items unrecoverable.
```

Every artifact gets a manifest entry:

```yaml
artifact: startup-config
status: inaccessible
reason: authentication_required
risk_if_continuing: may_be_lost
```

Status is one of `captured`, `inaccessible`, `unknown` or `absent`, each with a
reason and a stated risk. A `PARTIAL` archive does not block recovery — it
informs the confirmation prompt, so the operator decides knowing precisely what
they may be about to lose.

### 10.2 Three concepts that must not be blurred

| Concept | What it does | What it claims |
| --- | --- | --- |
| **Archive** | Captures what is observable before change, and discloses the gaps | Nothing *readable* is lost, and the unreadable is named |
| **Reset** | Known-empty lab state — `write erase`, `vlan.dat`, reload | The device is ready to learn on |
| **Sanitise** | Verified destruction of residual data | Claimed **nowhere**, by default |

Used network equipment demonstrably retains previous-owner configurations,
credentials, addressing and cryptographic material. `archive` runs before
`reset` by default; a bare `reset` on an unarchived device warns and requires
confirmation. Media sanitisation has a real standard (NIST SP 800-88 Rev. 2) and
a hobby tool should not imply conformance it has not demonstrated.

### 10.3 The recovery-disabled interlock

Cisco documents a **confirmation path** for devices with
`no service password-recovery`: the user is told recovery is disabled and asked
whether to reset to the default configuration; accepting erases the startup
config, declining lets the device continue booting.

`DESTRUCTIVE_RECOVERY_GUARD` is entered on **observed evidence** of that prompt.
ciscoyoke refuses by default, states precisely what accepting would cost —
including which archive artifacts are still `inaccessible` and would be lost —
and requires `--accept-config-loss` plus a confirmation challenge.

#### Confirmation identity

Revision 3 required the device serial number typed out. That contradicts §10.1:
on a locked device the serial is frequently *not observable*, and a tool cannot
demand a value it has just admitted it does not know.

The challenge is therefore built from **the strongest independently observed
attributes available**. Best case:

```
Type device serial FOC0912X1AB:
```

Fallback, when identity is not observable:

```
Device serial is not observable.

You are about to erase configuration from:

  TARGET       bench-left
  PORT         COM3
  USB ADAPTER  FTDI A10K8Z4P
  DEVICE       Catalyst bootloader
  MODEL        unknown
  STATE        DESTRUCTIVE_RECOVERY_GUARD

Type:  ERASE bench-left COM3
```

The challenge still cannot be satisfied by the wrong device — it names the
physical adapter — while remaining answerable on a device that will not identify
itself. That is commitment 1 applied to the confirmation prompt.

Precision is itself a mitigation: a tool that overstates a hazard, or demands
impossible confirmations, trains users to work around it.

### 10.4 Credential lifecycle

Credentials must never reach a command line:

```
ciscoyoke rescue COM3 --password Cisco123      # never supported
```

because that lands in shell history, process listings, and any support bundle
that captures the invocation. Interactive prompts use `getpass`:

```
Username:        admin
Password:        ********
Enable password: ********
```

For automation, `--password-stdin`, and later a secret-provider interface.

Hard rules, each enforced rather than documented:

- Credentials are never written to the journal.
- Credentials are never written to a `--json` result.
- Credentials never appear in exception strings or log events.
- TX events carrying a credential are **marked secret before the recorder
  receives them**, not redacted afterwards.
- Support bundles can never include a credential object.

> **Invariant: no secret supplied through the credential interface appears in
> any journal, JSON result, support bundle, log event or public transcript.**

Property-tested by supplying a distinctive sentinel credential, exercising the
lifecycle, and asserting the sentinel appears in none of those artifacts.

### 10.5 Transcript privacy

Secrets travel in **both directions**. Revision 2 handled TX — credentials the
tool itself sends. But RX carries far more:

```
username admin secret 5 $1$...
enable secret 5 $1$...
snmp-server community ...
crypto key ...
tacacs-server key ...
radius-server key ...
```

Design:

- **Secret-aware at record time**, TX and RX both. Known credential-bearing
  patterns are marked as they are written.
- **Raw is private by default.** Restrictive permissions, gitignored, never
  loadable as a fixture — the formats have different schemas.
- **`.ytx.pub` scrubbing inspects RX aggressively**, not just the tool's own
  transmissions.
- **Deterministic and regression-tested.** Scrubbing is a pure function.
- **Provenance recorded**, so CI fixtures are traceable to a scrub version.

And it never promises a clean result:

```
SCRUB RESULT

  12 credential-like values redacted
   3 IPv4 addresses anonymised
   6 hostnames anonymised
   1 private key block removed

WARNING: automated scrubbing cannot prove a transcript contains no sensitive
data. Review before publishing.
```

### 10.6 General guards

- **Dry-run by default.** Every destructive command prints the exact bytes it
  would send, and exits. `--confirm` executes.
- **Destructive-effect metadata.** Every step declares what it can destroy; a
  plan's total effect is shown before it runs.
- **No implicit fan-out.** One port per command; `--all` must be typed.
- **Everything is transcribed.**

---

## 11. Image rescue

The hero feature. A device stranded at `rommon` or `switch:` with a missing or
corrupt image looks dead, and reviving it by hand is miserable, model-specific
and progress-blind — Cisco's own guidance notes an XMODEM transfer can take up
to two hours, and recommends raising the console to 115200 to speed it up.

### 11.1 Workflow

1. Capture and classify the bootloader state; initialise flash where required.
2. **Inventory flash and boot variables before deleting anything.**
3. If a valid alternate image is already present, offer the non-transfer boot
   path first — Cisco's own documentation recommends checking for an existing
   valid image before transferring, and the fastest fix is often no transfer.
4. Plan the transfer against available flash (§11.3).
5. Transfer by a capability-supported path — XMODEM over console, or TFTP
   (§11.4).
6. Expose progress, byte counts and estimated time. A two-hour transfer with no
   feedback is where people pull the power.
7. Prove the boot (§11.2), then compensate every temporary mutation.

### 11.2 Four different things called "verification"

Revision 2 said "compute SHA-256", which proves only *these are the bytes the
user gave us*. Four distinct claims, never conflated:

**Image fingerprint.** `SHA-256` of the local file. Establishes provenance and
lets a repeat run recognise the same image. Proves nothing about the image.

**Compatibility evidence.** Assembled, weighted, and reported with a confidence
— never asserted from a filename:

```
filename implies    Catalyst 2950
device identified   WS-C2950-24        (observed, high)
flash required      2.9 MiB
flash available     3.1 MiB            (observed)
DRAM requirement    unknown            (not derivable from the image)
compatibility       medium confidence
```

**Transfer integrity.** XMODEM provides protocol-level error detection with
per-block checksums; TFTP and on-device copies use on-device verification where
the platform supports it. This proves the bytes arrived, not that they are the
right bytes.

**Boot proof.** The only definitive success criterion:

```
✓ image loaded
✓ IOS reached expected state
✓ show version reports the expected image and version
✓ console baud restored to 9600
```

Separating hashing from authenticity from compatibility from operational proof
is the difference between a tool that says "verified" and one that says what it
actually knows.

### 11.3 The dangerous case is flash, not transfer

```
flash total    8 MB
existing IOS   5 MB
new IOS        5.5 MB
free           2 MB
```

Deleting the current image makes room. It also means a failed transfer leaves
**zero bootable images** and a genuinely dead device — worse than the state the
user started in.

**Policy: ciscoyoke never deletes the only known-bootable image to make room
for a replacement without a separate, explicit risk acknowledgement.**

```
BLOCKED

The supplied image cannot be staged alongside the only known bootable image.

Existing bootable image:  flash:c2950-old.bin  (5.0 MiB, boots: verified)
Supplied image:           ios.bin              (5.5 MiB)
Free after deletion:      7.0 MiB — sufficient
Risk:                     no rollback image during transfer

Options:
  1. Abort                                        (default)
  2. Boot and retain the existing image instead
  3. Accept stranded-device risk and replace it   --accept-stranded-risk
```

Enforced as an invariant (§12.2), not a warning.

### 11.4 TFTP: bundled, and tightly constrained

Previously an open question; resolved. Cisco's 2600 recovery documentation
describes TFTP as the fastest recovery path and requires ROMMON variables such
as `IP_ADDRESS`, `TFTP_SERVER` and `TFTP_FILE`. Making the user download a
random twenty-year-old Windows TFTP executable mid-rescue defeats the purpose of
the tool.

`tftpy` (MIT, Python ≥3.8, actively released — 0.8.7, February 2026) provides a
server. ciscoyoke wraps it under strict constraints:

```
$ ciscoyoke recover image COM3 --image ios.bin --via tftp

Selected host interface   Ethernet 192.168.1.10
Temporary TFTP server     192.168.1.10:69
Serving                   ios.bin only
Write access              disabled
Directory traversal       impossible
Lifetime                  this recovery session only
```

Bound to one chosen interface — never `0.0.0.0` — serving exactly one file
read-only, torn down when the transfer completes, fails, or the process exits.
The constraints are themselves a security-design story, and they are tested.
Note that tftpy's own `listen()` defaults to *"INADDR_ANY (all interfaces) and
UDP port 69"*, so every one of these constraints is something ciscoyoke imposes
deliberately rather than inherits.

#### Privilege is a preflight check, not a `sudo` instruction

UDP 69 is below 1024, and on Linux binding a privileged port requires
`CAP_NET_BIND_SERVICE` or equivalent. **The wrong fix is telling users to run
ciscoyoke as root** — this process handles configs, raw transcripts, journals
and user-supplied firmware images, and none of that should run elevated.

Capability is therefore probed by `doctor` and reported, never assumed:

```
$ ciscoyoke doctor

Embedded TFTP
  interface bind       ✓
  UDP/69 bind          ✗ permission denied
  firewall             unknown

Embedded TFTP unavailable.

Options:
  - grant bind-service capability to an approved helper
  - use an external TFTP server        --tftp external://192.168.1.5
  - use XMODEM where supported         --via xmodem
```

The clean architecture is a provider interface:

```
TftpProvider
├── EmbeddedTftpProvider    the pleasant default, where privilege allows
└── ExternalTftpProvider    point at a server the user already runs
```

Host firewalls are the other real-world wrinkle: TFTP recovery failing because
the host firewall silently dropped UDP is a well-documented Cisco support
scenario. `doctor` reports firewall state as `unknown` rather than pretending to
have checked it, and a stalled transfer names the firewall as the first thing to
investigate.

This is commitment 4 in miniature: what can be verified is verified, and what
cannot is stated.

### 11.5 The legal boundary

Cisco software entitlement on second-hand equipment is not a problem this
project can solve. **ciscoyoke accepts a user-supplied image and automates
transport and verification. It never hosts, mirrors, searches for or
redistributes IOS images**, and no feature accepts a URL to fetch one from.
Lawful entitlement is the user's responsibility, stated plainly in the docs.

---

## 12. Testing

If the answer to *how is this tested?* is "you need a 2950 on your desk", it is
a toy.

### 12.1 Transcript record and replay

Every session records to a versioned `.ytx` file — JSONL, one record per I/O
event: direction, monotonic timestamp, raw bytes, redaction markers.

```json
{"v": 1, "t": 0.000, "dir": "rx", "b": "\r\nWould you like to enter the initial configuration dialog? [yes/no]: "}
{"v": 1, "t": 1.204, "dir": "tx", "b": "no\r"}
{"v": 1, "t": 1.510, "dir": "rx", "b": "\r\nPress RETURN to get started!\r\n"}
```

`FakeDevice` replays a recording as a serial port, feeding recorded `rx` bytes
and asserting the tool transmits the recorded `tx` bytes. Timestamps preserved
so timeout behaviour is exercised; waits compressible so a four-minute reload
tests in milliseconds. **The full suite runs green in CI with no hardware
attached.**

### 12.2 Property and invariant tests

Stream and state layers are pure, so Hypothesis applies directly.

**Chunk-boundary invariance:**

```python
@given(transcript=known_transcripts(), splits=st.lists(st.integers(0, 4096)))
def test_state_detection_is_chunk_invariant(transcript, splits):
    assert states(fragment(transcript, splits)) == states([transcript])
```

**Safety invariants**, each traceable to a hazard, asserted over all reachable
playbook executions:

| Invariant | Guards |
| --- | --- |
| No destructive step executes while `DESTRUCTIVE_RECOVERY_GUARD` is reported | §10.3 |
| No destructive step executes before `archive` completes, absent explicit override | §10.1 |
| The only known-bootable image is never deleted without `--accept-stranded-risk` | §11.3 |
| Every step is guarded by an expect; none fires into an unverified state | §5.2 |
| A playbook whose transport requirements exceed declared capabilities fails at plan time | §7.1 |
| Every mutation declares a reversibility class before it is attempted | §9.1 |
| Every `REVERSIBLE` or `COMPENSATABLE` mutation carries a compensation | §9.1 |
| No `IRREVERSIBLE` mutation executes without passing its named safety guard | §9.1 |
| No journal entry in `ATTEMPTED` is treated as fact without observation | §9.2 |
| Journal replay is idempotent: resuming twice equals resuming once | §9.4 |
| At most one mutating session owns a physical transport path at a time | §9.5 |
| A lease held by a dead process is reclaimed without manual intervention | §9.5 |
| No credential appears in a journal, JSON result, support bundle, log or public transcript | §10.4 |
| Scrubbing is deterministic; identical input yields identical output | §10.5 |
| No raw `.ytx` is loadable as a fixture | §10.5 |
| The bundled TFTP server binds one interface, serves one file, and exits | §11.4 |

### 12.3 Support matrix with evidence semantics

A community transcript is not equivalent to hardware verification, and for
destructive operations the difference matters enormously.

```
● Hardware verified      run against real hardware by a maintainer
◐ Replay verified        passes against a committed transcript, no hardware run
○ Documentation-derived  implemented from official docs, never executed
— Unsupported / not yet attempted
```

**Target state at v1.0 — nothing is implemented, so nothing is earned.** This
spec claims no support whatsoever; the marks below are the *ambition*, and the
README matrix will be generated from real evidence as it accrues. Publishing a
matrix full of ● while the spec's own status line says "nothing implemented"
would contradict commitment 1 in the most visible place in the repository.

| Platform | Intake | Access recovery | Image rescue | Reset |
| --- | --- | --- | --- | --- |
| Cisco 1760 | — | — | — | — |
| Catalyst 2950 | — | — | — | — |
| Catalyst 3550 | — | — | — | — |
| Catalyst 2960 | — | — | — | — |
| 2600 / 2800 | — | — | — | — |

Every ● and ◐ will link to its transcript fixture; every ○ to the official
document it was derived from. A reviewer sees immediately that the author knows
the difference between tests passing and reality being verified — and that the
circles were earned one at a time.

---

## 13. The lab file

Containerlab's structure, adapted for hardware you cannot instantiate.

```yaml
name: ccna-ospf-single-area
description: Two routers, two switches, OSPF area 0 across a trunk.

devices:
  r1:
    match: { model: "CISCO1760", serial: "FTX0812A1BC" }
    config: configs/r1.cfg
  r2:
    match: { model: "CISCO1760" }
    config: configs/r2.cfg
  sw1:
    match: { label: "bench-left" }     # stable adapter label, §7.3
    config: configs/sw1.cfg

cabling:
  - [r1:FastEthernet0/0, sw1:FastEthernet0/1]
  - [r2:FastEthernet0/0, sw1:FastEthernet0/2]
  - [sw1:FastEthernet0/24, sw2:FastEthernet0/24]
```

`match` resolves in specificity order: device serial, model, adapter label,
port. **Matching carries confidence** — a low-confidence identity match on a
device about to be erased is refused, not guessed. Ambiguity is an error naming
both candidates. A declared device not on the bench is reported before anything
is written anywhere: apply is all-or-nothing at plan time.

### 13.1 Cabling verification

```
✓ r1:Fa0/0        ── sw1:Fa0/1
✗ r2:Fa0/0        ── sw1:Fa0/3        expected sw1:Fa0/2
? sw1:Fa0/24      ── (no neighbour)   expected sw2:Fa0/24
! sw2:Fa0/24      ── sw2:Fa0/23       loop: both ends on the same device
```

Four outcomes, because "wrong" is not actionable and each implies a different
fix. CDP works at layer 2 and needs no addressing, so cabling is verifiable on a
bench of freshly-erased devices.

**Any CDP or configuration change made to verify is journalled and restored
afterwards** (§9). A verification tool that leaves the device changed is a
misconfiguration tool.

---

## 14. Command surface

```
ciscoyoke rescue TARGET [--image BIN]     the front door — orchestrates the lifecycle
ciscoyoke doctor                          host serial/driver/permission/capability diagnostics
ciscoyoke ports [label PORT NAME]         adapter identity and stable labels
ciscoyoke scan                            enumerate endpoints; observed facts and confidence
ciscoyoke intake TARGET                   passive-first identity, boot and health evidence
ciscoyoke archive TARGET -o DIR           preservation bundle, manifest, disclosed gaps
ciscoyoke recover access TARGET           access/password recovery playbook
ciscoyoke recover image TARGET --image B  user-supplied image rescue
ciscoyoke reset TARGET                    known-empty lab baseline
ciscoyoke health TARGET                   POST, flash, memory, environment checks
ciscoyoke console TARGET                  interactive console with safe recording
ciscoyoke transcript scrub FILE           deterministic share-safe fixture
ciscoyoke lab apply LABFILE
ciscoyoke lab verify LABFILE
ciscoyoke support-bundle                  scrubbed transcript, state timeline, host metadata
```

`TARGET` is a port, a label (§7.3), or a device serial.

The executable and package are both `ciscoyoke`; `yoke` is taken on PyPI by an
unrelated AWS Lambda deployer.

### 14.1 `rescue` is the front door

Someone who bought a mystery Catalyst does not want to learn an internal
lifecycle first. `rescue` runs the primitives and reports a plan:

```
$ ciscoyoke rescue COM5

RESCUE PLAN — COM5

  1. Diagnose console               ✓
  2. Identify current state         BOOTLOADER (high)
  3. Preserve available evidence    PARTIAL — 4 captured, 2 inaccessible
  4. Inspect flash                  ✓
  5. Look for bootable image        none found
  6. Recovery required              image rescue

Nothing destructive has happened.

Recommended next action:
  ciscoyoke rescue COM5 --image <ios-image.bin>
```

With an image supplied, it plans the whole operation before touching anything:

```
$ ciscoyoke rescue COM5 --image c2950-i6q4l2-mz.121-22.EA14.bin

PLAN

  Device             Catalyst 2950 (WS-C2950-24, observed)
  Current state      switch:
  Preservation       partial — startup-config inaccessible, may be lost
  Image supplied     c2950-i6q4l2-mz.121-22.EA14.bin
  Image hash         4f2a...c19d
  Compatibility      medium confidence
  Flash              3.1 MiB free / 2.9 MiB required — fits, no deletion needed
  Transfer           XMODEM @ 115200
  Estimated time     ~11 minutes
  Temporary changes  console baud 9600 → 115200  (compensation recorded)
  Postconditions     IOS prompt, expected version, baud restored

Proceed? [y/N]
```

The granular verbs remain for advanced use. `rescue` is what turns the
architecture into a product, so a **minimal version ships in P0** — diagnose,
infer state, archive, and recommend a recovery path, without image rescue:

```
$ ciscoyoke rescue bench-left

  ✓ transport diagnosed
  ✓ device state inferred      LOGIN_PASSWORD (high)
  ✓ evidence archived          PARTIAL — 3 captured, 3 inaccessible
  ! access unavailable

Recommended recovery:
  Catalyst password recovery — guided, requires physical Mode button

Run with --confirm to continue.
```

P1 gives that same command image-rescue powers. The shipping UX is therefore
present from the first release rather than bolted on later.

### 14.2 Failure UX

Every failure reports, in order: **observed state, expected state, attempted
action, refusal reason, safest next step.**

### 14.3 `scan` is honest about uncertainty

```
$ ciscoyoke scan
4 endpoints found

PORT   LABEL         ADAPTER    STATE       PLATFORM        MODEL       BAUD   CONFIDENCE
COM3   bench-left    FTDI       priv-exec   Cisco IOS       CISCO1760   9600   high
COM4   bench-right   FTDI       login       Cisco IOS       unknown     9600   medium
COM5   —             FTDI       switch:     Catalyst boot   unknown     9600   high
COM7   —             Prolific   silent      unknown         unknown     —      low

COM4  identity requires boot evidence or credentials
COM5  bootloader reached             run: ciscoyoke rescue COM5
COM7  no signal at candidate rates   run: ciscoyoke doctor --port COM7
```

Every uncertain line says *why*; every problem line carries the command for it.

### 14.4 Machine interface, from day one

Every command accepts `--json` and emits a versioned result schema:

```json
{
  "schema_version": 1,
  "command": "scan",
  "devices": [
    {
      "endpoint": "COM3",
      "label": "bench-left",
      "state": "priv_exec",
      "basis": "observed",
      "confidence": "high",
      "evidence": [
        {
          "signal": "prompt",
          "value": "Router#",
          "context": "line_end_after_quiescence"
        }
      ],
      "model": "CISCO1760",
      "model_basis": "observed",
      "model_evidence": "show_version"
    }
  ]
}
```

**Confidence is ordinal, not numeric.** A `state_confidence` of `0.98` looks
scientific without being probabilistic — it invites arithmetic nobody has earned
the right to do. Until the detector is calibrated against a labelled corpus,
there is no honest number, so the schema carries what the tool actually has:

```
basis:       observed | inferred | unknown
confidence:  high | medium | low
evidence:    [ the signals that produced the conclusion ]
```

Calibrating real probabilities from an accumulated transcript corpus would be a
genuinely interesting later release. Asserting them now would be commitment 1
violated in the machine-readable output.

This makes ciscoyoke usable from shell scripts, CI, other Python tools, Ansible
wrappers and any future TUI — and it makes the architecture look considerably
less like a weekend CLI.

### 14.5 Exit codes

```
 0  success
10  device not found
11  state uncertain
20  authentication required
30  destructive action refused
40  transport capability unavailable
50  transfer failed
60  interrupted recovery requires resolution
70  target lease held by another session
```

Documented, stable, and covered by tests.

---

## 15. Roadmap

| Tier | Build | Why |
| --- | --- | --- |
| **P0** | `doctor`; `ports` with stable labels; confidence-aware `scan`; stream/signals/tracker; private-safe transcripts and replay; `intake`; `archive` with disclosure; write-ahead journal on SQLite; transport leases; credential lifecycle; **minimal `rescue`**; one router and one guided switch access-recovery path; `--json` and exit codes | A coherent, safe, restartable, machine-readable product before the flashiest feature |
| **P1** | `rescue` gains image rescue on one router and one switch family, with flash planning and bundled TFTP; `reset`; `health`; RFC2217 transport | Turns it into a resurrection bench |
| **P2** | Lab binding; concurrent apply; CDP diff; automation inventory handoff | The memorable demo and the repeatable lab workflow |
| **Defer** | Multi-vendor; web UI; cloud; AI diagnosis; large feature database; blanket sanitisation | Breadth dilutes the engineering story before the core is proven |

### 15.1 Milestones

- **M0 — Design before code.** Support philosophy, threat model, state diagram,
  transcript schema, result schema, mocked 90-second demo.
- **M1 — "I can tell what is on my bench."** `doctor`, `ports`, `scan`,
  `intake`, fixtures, property tests, cross-platform CI.
- **M2 — "I can safely get control of it."** One router and one switch recovery
  path, with `archive`, human-action steps and the journal.
- **M3 — "I can rescue a genuinely broken box."** Image rescue with flash
  planning, transfer progress, the four verifications and boot proof.
- **M4 — "I can turn a pile of junk into a repeatable lab."** `reset`,
  `lab apply`, CDP verify, inventory handoff.

---

## 16. What the repository must show

The employer-impressing part is not the number of supported switches. It is that
someone can open the repository and see what was chosen, what was refused, what
can go wrong, and how the dangerous paths are proven safe.

These are **planned artifacts, written alongside the code that justifies them**,
not up front. An ADR describes a decision made under pressure with real
trade-offs; a directory of them written before any implementation is
documentation theatre.

- **ADRs** for transport/state separation, capability tri-state, journal design,
  Cisco-first scope, transcript format, firmware policy and destructive-operation
  semantics. → `docs/adr/`, from M1
- **A threat model** covering wrong-device selection, accidental fan-out,
  destructive recovery, previous-owner data, transcript secrets, stranded
  devices and firmware licensing. → `docs/THREAT-MODEL.md`, at M0; the hazards
  are already specified in §10–§11 and the invariants enforcing them in §12.2
- **CI** running the real lifecycle engine against scrubbed recorded hardware
  behaviour, plus Hypothesis chunk-boundary and safety invariants
- **A support matrix** with the evidence semantics of §12.3
- **Failure UX** as specified in §14.2
- **`support-bundle`** producing a scrubbed transcript, state timeline, journal,
  version, host and adapter metadata — so bug reports are reproducible
- **Professional packaging:** `pyproject.toml`, mypy strict, ruff, semantic
  releases, changelog, dependency/SBOM scanning, `pipx install ciscoyoke`
- **A contribution workflow:** capture → scrub → replay → add playbook. Other
  people's obsolete boxes become a growing compatibility corpus, instead of the
  maintainer needing to own every platform

> **Portfolio rule.** A small support matrix with impeccable evidence, safety and
> replayability reads as more senior than claimed support for fifty platforms
> nobody owns.

---

## 17. The README demo

One complete resurrection narrative:

**unknown boxes → one locked login → one bootloader with a missing image →
preserve what can be preserved → guided recovery → IOS restored → reset →
deliberately swap one patch lead → `lab verify` pinpoints the mismatch.**

```
$ ciscoyoke scan
4 endpoints found
COM3  bench-left   console responsive   Cisco IOS       model unknown       9600
COM4  bench-right  login prompt         Cisco IOS       locked              9600
COM5  —            switch:              Catalyst boot   image not booting   9600
COM7  —            silent               unknown         check power/cable   —

$ ciscoyoke rescue COM5
State: BOOTLOADER (high)
Boot image: configured but missing
Preservation: PARTIAL — 4 captured, 2 inaccessible
Nothing destructive has happened.
Recommended: ciscoyoke rescue COM5 --image <ios-image.bin>

$ ciscoyoke rescue COM5 --image c2950-i6q4l2-mz.121-22.EA14.bin
Flash 3.1 MiB free / 2.9 MiB required — fits, no deletion needed
Transfer XMODEM @ 115200, ~11 min, baud restored on completion
Proceed? [y/N] y
...
PASS: image loaded, IOS reached, version verified, baud restored

$ ciscoyoke lab verify lab.yaml
✗ r2:Fa0/0 ── sw1:Fa0/3    expected sw1:Fa0/2
```

Serial I/O, state machines, Cisco internals, systems design, security thinking,
testing, automation and product UX — in about ninety seconds.

---

## 18. Open questions

- **Concurrency model.** Threads versus asyncio for driving N ports at once.
  Per-port blocking reads suit threads; asyncio suits the RFC2217 transport.
  Decide before P2.
- **Config-register semantics beyond 0x2142/0x2102.** A general facility, or is
  a two-value abstraction the honest scope?
- **Health check depth.** How much POST, flash and memory diagnostics is worth
  parsing per platform before returns diminish?

Three open questions, none of which block M0. **Everything else is decided.**

*Resolved in revision 4:* mutation reversibility classes (§9.1), write-ahead
journal semantics and SQLite storage (§9.2), journal location (§9.3), transport
leases (§9.5), credential lifecycle (§10.4), confirmation identity fallback
(§10.3), TFTP privilege preflight (§11.4), ordinal confidence (§14.4), honest
support matrix (§12.3), minimal `rescue` in P0 (§14.1).
*Resolved in revision 3:* TFTP bundling (§11.4), image verification semantics
(§11.2), archive honesty (§10.1), re-entrancy (§9), capability tri-state (§7.1),
adapter identity (§7.3), machine interface (§14.4).
*Resolved in revision 2:* transcript redaction, remote transport, firmware
licensing boundary.

---

### 18.1 What happens next

The spec stops here. The next artifact is code: M0 (threat model, state diagram,
transcript and result schemas, mocked demo), then M1 (`doctor`, `ports`, `scan`,
`intake`, fixtures, property tests, CI). Design questions arising after this
point get answered by running against real Cisco hardware, not by extending this
document — which is also what a prospective employer should see in the commit
history.

---

## 19. References

**Cisco platform documentation**
- [Troubleshoot password recovery in IOS and IOS XE routers](https://www.cisco.com/c/en/us/support/docs/ios-nx-os-software/ios-xe-16/217045-troubleshoot-password-recovery-in-cisco.html)
- [Recover password for Catalyst fixed-configuration switches](https://www.cisco.com/c/en/us/support/docs/switches/catalyst-2950-series-switches/12040-pswdrec-2900xl.html)
- [ROMmon recovery for the Cisco 2600 series and VG200](https://www.cisco.com/c/en/us/support/docs/routers/2600-series-multiservice-platforms/15079-recovery-c2600.html) — TFTP as fastest path; `IP_ADDRESS`, `TFTP_SERVER`, `TFTP_FILE`
- [Recover legacy Catalyst fixed switches from corrupt images](https://www.cisco.com/c/en/us/support/docs/switches/catalyst-2950-series-switches/41845-192.html) — XMODEM duration, 115200 acceleration
- [No service password-recovery](https://www.cisco.com/c/en/us/td/docs/ios/12_4/12_4_mainline/nsps.html) — the confirmation path, §10.3
- [Software transfer and re-licensing policy](https://www.cisco.com/c/en/us/products/collateral/services/high-availability/qa_c67-475667.html) — §11.5

**Adjacent tooling**
- [pyATS / Genie Clean device recovery](https://pubhub.devnetcloud.com/media/genie-docs/docs/clean/user_guide/writing_a_clean/device_recovery.html) and [pyATS 24.8 release notes](https://developer.cisco.com/docs/pyats/24-8/) — §3
- [ConsolePi](https://consolepi.readthedocs.io/en/latest/), [ser2net](https://github.com/cminyard/ser2net), [netmiko](https://github.com/ktbyers/netmiko)
- [containerlab topology definition](https://containerlab.dev/manual/topo-def-file/) — lab file prior art
- [NVIDIA Prescriptive Topology Manager](https://docs.nvidia.com/networking-ethernet-software/cumulus-linux-514/Layer-1-and-Switch-Ports/Prescriptive-Topology-Manager-PTM) — cabling verification prior art

**Libraries and standards**
- [pySerial `list_ports`](https://pyserial.readthedocs.io/en/stable/tools.html) — VID/PID/serial/location, §7.3
- [pySerial URL handlers](https://pyserial.readthedocs.io/en/stable/url_handlers.html) — `socket://` ignores control lines; neither handler is encrypted or authenticated, §7.2
- [pySerial `send_break` duration](https://github.com/pyserial/pyserial/issues/397)
- [tftpy](https://pypi.org/project/tftpy/) — MIT, Python ≥3.8, 0.8.7 (Feb 2026); `listen()` defaults to INADDR_ANY and UDP 69, §11.4
- [platformdirs](https://pypi.org/project/platformdirs/) — 4.11.7, OS state directory resolution, §9.3
- [capabilities(7)](https://man7.org/linux/man-pages/man7/capabilities.7.html) — `CAP_NET_BIND_SERVICE` for UDP 69, §11.4
- [ntc-templates](https://github.com/networktocode/ntc-templates)
- [NIST SP 800-88 Rev. 2](https://csrc.nist.gov/pubs/sp/800/88/r2/ipd) — media sanitisation, §10.2
- [ESET: second-hand router residual-data study](https://www.eset.com/blog/en/how-to-dispose-of-old-routers-properly/) — why `archive` precedes `reset`
