# ciscoyoke — threat model

What can go wrong, who it hurts, and which invariant prevents it.

This is not a security-product threat model. ciscoyoke performs irreversible
operations on irreplaceable hardware, handles other people's residual data, and
produces artifacts that get published to GitHub. Those three facts generate real
hazards, and this is where they are enumerated rather than discovered by a user.

**Scope.** One operator, on their own machine, with physical access to the
devices. Not modelled: a malicious operator — they already hold a console cable
and ciscoyoke grants no privilege they lack — or a hostile serial device
attacking the host, beyond T7.

**Status.** Every mitigation below is marked ✅: implemented and covered by a
test today. Nothing here is claimed on the strength of intent.

That is not the same as saying the hazards are eliminated. Each section ends
with its residual risk, and the largest one is stated once here: **no Cisco
device has ever been connected to this code.** Every guard is verified against
recorded and constructed behaviour, not against a real 2950 refusing to do what
its documentation says. The first hardware run is expected to find things.

---

## Assets

| Asset | Why it matters |
| --- | --- |
| The physical device | Irreplaceable; out of production, no support |
| Its existing configuration | May be the only copy; may belong to a previous owner |
| Previous-owner residual data | Credentials, addressing, keys — not the operator's to leak |
| Transcripts | Faithful recordings of everything the console showed |
| Published fixtures | Committed to a public repository, permanently |
| The operator's host | Serial permissions, TFTP surface, supplied firmware |

---

## T1 — Wrong-device selection

**Scenario.** Four identical Catalyst switches on the bench. `ciscoyoke reset
COM4` is run, but COM enumeration shuffled after a reboot and COM4 is now the
switch holding the configuration built over the previous evening.

**Impact.** Irreversible loss of the wrong device's configuration.

**Mitigations.**
- ✅ Ports are identified by USB descriptor, not by the name the OS assigned
  this boot. `PortIdentity.key` prefers the adapter serial number, falls back to
  physical location, and only then to the port name.
- ✅ Identity strength is reported (`serial` / `location` / `name_only`) rather
  than implied, so the operator can see when recognition is weak.
- ✅ `find_ambiguities` groups adapters that genuinely cannot be told apart —
  two no-serial Prolific clones — so a label can be refused rather than guessed.
- ✅ Low-confidence identity on a device about to be written to is refused. A
  binding is only as strong as its weakest half, so a serial-number selector
  resolving to an unrecognisable port is downgraded rather than trusted.
- ✅ A plan is checked and rendered without sending anything: `plan()` reports
  what a playbook would destroy, and `execute()` refuses a destructive plan
  without explicit confirmation.

**Residual risk.** A device whose identity cannot be read before authentication
can only be matched by port. The tool says so explicitly.

---

## T2 — Accidental fan-out

**Scenario.** One device was meant; the bench is reset.

**Impact.** T1, multiplied.

**Mitigations.**
- ✅ Commands act on one port. There is no fan-out flag at all yet, which is the
  strongest form of this guarantee.
- ✅ `lab apply` resolves every declared device before writing to any, and
  refuses an incomplete plan — a half-applied topology across four boxes leaves
  nobody able to say which half.
- ✅ Every unresolved device is reported at once rather than one per rerun.
- ✅ A device bound only by port name is refused for writing: a port name
  identifies whatever enumerated there this boot, not a specific device.

---

## T3 — Destructive recovery

**Scenario.** Access recovery is run on a device configured with
`no service password-recovery`. Accepting the prompt erases the startup
configuration — possibly the only record of how a just-bought device was set up.

**Impact.** Irreversible loss of a configuration never read.

**Mitigations.**
- ✅ `DESTRUCTIVE_RECOVERY_GUARD` is entered on observed evidence of the
  platform's banner, and **outranks every prompt reading**: a bootloader prompt
  arriving afterwards cannot clear it. Tested directly, and as a property under
  arbitrary stream chunking, because a guard that a read boundary could hide is
  not a guard.
- ✅ Override requires `--accept-config-loss`, and destructive commands dry-run
  by default: the plan, the preservation gaps and the exact effects are printed
  and nothing is sent without `--confirm`.
- ✅ No step with destructive effect executes while the guard is reported, and
  the guard is re-checked **before every step** rather than only at plan time —
  a device can announce recovery is disabled part-way through a boot, after the
  plan was made and before the damaging step is reached.

**Design note.** Revision 1 of the spec claimed any break permanently destroys
the config. Cisco documents a confirmation path instead. Overstating a hazard
trains users to ignore warnings, so accuracy is itself a mitigation.

---

## T4 — Previous-owner data

**Scenario.** A router bought on eBay contains the previous owner's internal
addressing, SNMP communities, VPN pre-shared keys and TACACS secrets. It is
erased without being read — or worse, published.

**Impact.** Disclosure of a third party's network data. The operator is not the
injured party, which is precisely why the tool must care.

**Mitigations.**
- ✅ `reset` is refused outright on a device that has not been archived, unless
  the loss is accepted explicitly.
- ✅ Archive reports `PARTIAL` and names every artifact it could not read, with
  the reason and what continuing would cost. A `PARTIAL` archive does not block —
  on a locked device it is the only possible outcome, and refusing would make the
  tool useless for its main case — but the cost is forced into the confirmation.
- ✅ Salvaged configuration is written with a warning header saying it may belong
  to a previous owner, because the file outlives the moment and the context.
- ✅ Cryptographic material is reported `unknown` rather than `absent`: not
  knowing is different from knowing there is nothing there.
- ✅ Salvaged configuration and raw recordings are gitignored by default.
- ✅ No blanket sanitisation claim is made anywhere. NIST SP 800-88 sets a real
  bar and this tool does not clear it.

**Residual risk.** ciscoyoke cannot guarantee residual data is gone from flash,
NVRAM, or any location it does not know about. It says so.

---

## T5 — Transcript secrets

**Scenario.** A transcript faithfully records a session in which an enable
password was typed, or in which `show running-config` scrolled past a VPN key.
It is committed as a fixture. It is now in git history, permanently.

**Impact.** Permanent public disclosure, unfixable after the fact — deleting the
file does not remove it from history.

**Mitigations.**
- ✅ The recorder never stores a credential it was told about: the plaintext is
  discarded before the record is constructed, so there is no window in which it
  exists on disk awaiting cleanup.
- ✅ Redaction is fixed-width, so transcript byte counts cannot leak password
  length.
- ✅ Raw and public transcripts are **different schemas**. `read(require_public=
  True)` raises on a raw recording, so a fixture loader cannot be handed one by
  mistake.
- ✅ A public transcript without scrub provenance fails to load — CI input whose
  history is unknown is not trusted.
- ✅ Scrubbing inspects RX, not just what the tool transmitted, covering
  `enable secret`, `username … secret`, SNMP communities, TACACS/RADIUS keys,
  line passwords, PEM and Cisco hex key blocks.
- ✅ Scrubbing is deterministic — a regenerated fixture produces no spurious
  diff.
- ✅ `.gitignore` and a CI job both refuse raw `.ytx` files in the repository.
- ✅ The scrub report always states that scrubbing cannot prove a transcript is
  clean.

**Residual risk.** A secret the tool did not send and cannot recognise — an
arbitrary string in a config dump — may survive. Hence raw recordings are
private by default and promoting one to a fixture is a deliberate act.

---

## T6 — Firmware licensing

**Scenario.** The tool becomes a convenient distribution channel for Cisco
software.

**Impact.** Legal exposure for the project and its users.

**Mitigations.**
- ✅ Stated non-goal in the specification and README: ciscoyoke never hosts,
  mirrors, searches for or redistributes IOS images.
- ✅ No feature accepts a URL to fetch an image from; `--image` takes a local
  path and nothing else.
- ✅ Documentation states plainly that lawful entitlement is the user's
  responsibility.

---

## T7 — Host-side exposure

**Scenario.** Image rescue over TFTP needs a server. `tftpy`'s own `listen()`
defaults to *"INADDR_ANY (all interfaces) and UDP port 69"* — so a careless
integration exposes a service on every interface for the length of a
possibly-two-hour transfer.

**Impact.** An unintended network service on the operator's machine.

**Mitigations.**
- ✅ `doctor` probes whether UDP/69 can be bound and **reports** the answer.
- ✅ When it cannot, the output offers an external TFTP server or XMODEM. It
  never suggests `sudo`, and a test asserts the word does not appear: this
  process handles transcripts, journals and firmware, and escalating all of it
  to bind one port is the wrong trade.
- ✅ Firewall state is reported as `unknown` rather than guessed, and named as
  the first thing to check when a transfer stalls.
- ✅ The embedded server binds one chosen interface — never `0.0.0.0` — serves
  exactly one file whose path is resolved before the server starts, and is torn
  down on completion, failure or exit. `tftpy`'s own defaults are the opposite
  on both counts, so each constraint is imposed rather than inherited.
- ✅ An `ExternalTftpProvider` exists precisely so a host that cannot bind the
  privileged port has a route that is not "run elevated".
- ✅ Console input is treated as untrusted bytes: decoded with latin-1, never
  evaluated, never used to build a shell command. A hostile device can produce a
  wrong *state reading*, not host code execution.

---

## T8 — Silent wrong answers

**Scenario.** `scan` reports a model and IOS version for a locked device it
could not actually identify. The operator trusts it and acts.

**Impact.** Every hazard above, entered through a false premise. The most
insidious failure, because nothing appears to go wrong.

**Mitigations.**
- ✅ Every conclusion carries a basis (`observed` / `inferred` / `unknown`), an
  ordinal confidence, and the signals that produced it.
- ✅ Confidence is never a float. A calibrated probability would require a
  labelled corpus that does not exist; a number without one only looks rigorous.
- ✅ `Finding.unknown(reason)` makes a non-answer carry why, so an unknown field
  is actionable rather than merely absent.
- ✅ A prompt-shaped string inside scrolling output does not read as a prompt —
  only one the device is sitting at, with nothing after it, does.
- ✅ `Password:` is resolved against what was last transmitted, and a cold
  attach with no context reports low confidence rather than guessing well.
- ✅ The support matrix is entirely `—`. Nothing is claimed before it is earned.

---

## T9 — Interrupted recovery

**Scenario.** The console is switched to 115200 for an XMODEM transfer. The
laptop sleeps, the cable is nudged, Python crashes. The device now sits at a
baud rate ciscoyoke chose, mid-recovery.

**Impact.** A device left in a state neither the tool nor the operator can
describe, and a second run that probes at the wrong speed and concludes the
device is dead.

**Mitigations.**
- ✅ Every mutation declares a reversibility class **in its constructor**, so an
  unsafe one cannot be built: a reversible or compensatable mutation without a
  compensation raises, and an irreversible one that fails to name its guard
  raises. An irreversible mutation that *claims* a compensation also raises —
  that promise is the one the earlier design got wrong.
- ✅ Write-ahead journal on WAL-mode SQLite with `synchronous=FULL`: intent is
  durable before anything is sent, and survives a process that dies mid-write.
- ✅ Leaving the `attempting()` block does **not** mark a mutation applied. It
  stays `ATTEMPTED` until the device is observed, because the sending code
  returning proves only that bytes left the host.
- ✅ Reconciliation resolves each unresolved entry by probing, promoting it to
  `OBSERVED_APPLIED` or demoting it to `FAILED`. Where the journal and the
  device disagree, the device wins.
- ✅ `INDETERMINATE` is a permitted answer and **blocks automatic resume**. A
  prober forced to choose applied-or-not would manufacture exactly the certainty
  this mechanism exists to avoid.
- ✅ Rollback compensates newest-first, and returns the irreversible mutations
  it could not undo rather than implying a full restore.

---

## T10 — Concurrent sessions

**Scenario.** Two terminals, one device. Or a forgotten background process still
holding the port.

**Impact.** Interleaved writes on one console and two journals disagreeing about
what was done — corruption of exactly the state that makes T9 recoverable.

**Mitigations.**
- ✅ A lease keyed on **transport identity**, not port name, so two labels
  resolving to one adapter collide correctly.
- ✅ Exclusivity does not depend on PID: two `Lease` objects in one process is
  the double-open bug this exists to catch, so a same-PID holder blocks like any
  other. Re-entrancy belongs to the object, not the process.
- ✅ Claims are taken with `O_EXCL`, so two processes racing between check and
  write cannot both succeed.
- ✅ A lease held by a dead process is reclaimed automatically, and a corrupt
  lock file is not treated as a valid claim. A crash never requires manual
  unlocking — otherwise people learn to delete lock files, at exactly the wrong
  moment.
- ✅ Releasing only ever removes our own claim.

---

## T11 — Credential leakage

**Scenario.** `--password Cisco123` on the command line, landing in shell
history, process listings and any support bundle capturing the invocation.

**Impact.** Credential disclosure through a channel nobody was watching.

**Mitigations.**
- ✅ No `--password` flag exists, and none will. Interactive entry via
  `getpass`; `--password-stdin` for automation, because a pipe reaches neither
  the process table nor the shell history.
- ✅ `Secret` refuses to render itself through `__repr__`, `__str__` or
  `__format__`, which covers the paths credentials actually escape by —
  f-strings, `print`, `logging`, exception messages. Reaching the plaintext
  requires `reveal()`, which is greppable and reviewable.
- ✅ `len()` returns the marker's length, so it cannot leak the real one.
- ✅ Asserted end to end with a sentinel credential driven through a transcript,
  a journal database (checked at the raw byte level), a JSON result, log records
  and an exception message.
- ✅ The recorder refuses to store a credential it is told about.

---

## Invariants

Each traceable to a hazard. ✅ is implemented and tested today.

| Invariant | Threat | |
| --- | --- | --- |
| State detection is invariant under byte-stream partition | T3, T8 | ✅ |
| The recovery-disabled guard cannot be cleared by a later prompt | T3 | ✅ |
| A prompt inside scrolling output is not read as a prompt | T8 | ✅ |
| No credential the recorder is told about reaches a transcript | T5, T11 | ✅ |
| Redaction is fixed-width and cannot leak length | T5 | ✅ |
| No raw `.ytx` is loadable as a fixture | T5 | ✅ |
| A public transcript without scrub provenance fails to load | T5 | ✅ |
| Scrubbing is deterministic | T5 | ✅ |
| Indistinguishable adapters are reported, never disambiguated by guess | T1 | ✅ |
| A playbook needing an unavailable capability fails at plan time | T3, T9 | ✅ |
| Diagnostics never recommend running elevated | T7 | ✅ |
| Every mutation declares a reversibility class before it is attempted | T9 | ✅ |
| No `ATTEMPTED` journal entry is treated as fact without observation | T9 | ✅ |
| An indeterminate mutation blocks automatic resume | T9 | ✅ |
| Reconciliation is idempotent: resuming twice equals resuming once | T9 | ✅ |
| Rollback compensates newest-first and reports what it cannot undo | T9 | ✅ |
| At most one mutating session owns a transport path | T10 | ✅ |
| A lease held by a dead process is reclaimed without manual intervention | T10 | ✅ |
| No destructive step runs before `archive` completes | T4 | ✅ |
| Every playbook step declares a precondition and an expectation | T1, T3 | ✅ |
| A destructive step cannot be declared without its mutation | T3, T9 | ✅ |
| The recovery guard is re-checked before every step, not only at plan time | T3 | ✅ |
| A destructive plan refuses to run without explicit confirmation | T1, T4 | ✅ |
| The only known-bootable image is never deleted without acknowledgement | T7, T9 | ✅ |
| A weakly-bound device is never written to | T1 | ✅ |
| `lab apply` refuses an incomplete plan before writing anything | T2 | ✅ |
| Destructive commands dry-run until `--confirm` | T1, T3, T4 | ✅ |
| An interrupted previous run blocks a new operation on that device | T9 | ✅ |
| No credential appears in any journal, result, bundle or log | T11 | ✅ |
