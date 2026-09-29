<!--
DRAFT community posts. Read each subreddit's rules on self-promotion first:
several allow a project post only occasionally, or only with a flair, or only
in a weekly thread. Lead with the story and ask for hardware reports, not stars.
Swap the install line for `pipx install ciscoyoke` once 0.1.0a1 is on PyPI.
-->

## r/homelab

**Title:** I built a tool for taking mystery Cisco switches from an eBay box to a
clean lab node over the console. The first real 2950 broke assumptions my
309-test suite never caught.

Old Catalysts are cheap, and they arrive with a stranger's config, a password
nobody knows, and sometimes no working IOS. Everything between "arrived" and
"Ansible can reach it" is done by hand over a console cable.

ciscoyoke does that part: it identifies what's on the cable, preserves the old
config before touching anything, walks you through the Mode-button password
recovery, and resets to a clean baseline. It proves each step from what the
switch actually printed rather than assuming.

The animation in the README is a real 2950 replayed from its recording. Plugging
it in found a silently failing rename, a bootloader that ends lines LF-CR, and a
log message hiding an IOS question. Write-up: [link to docs/launch/309-tests.md]

It's alpha. It's been run end to end on a 2950; the 2960 family is implemented
from Cisco's docs and has **never met one**. If you've got a 2960, 3560 or an
old ISR, a `ciscoyoke report` from it would help more than anything.

`pipx install git+https://github.com/Ryan-Clinton/ciscoyoke`, then
`ciscoyoke rescue COM4`, which is read-only.

## r/Cisco

**Title:** Open-source serial-console tool for password recovery and reset on
old Catalysts: looking for 2960/3560 testers

Same content, shorter, more technical: LF-CR bootloader lines, `CONFIG_FILE`,
stale `config.old` breaking the rename, IOS 15's autoinstall question, the USB
console driver on the 2960-S/X. Ask specifically for hardware reports.

## r/ccna

**Title:** If you've been given an old lab switch with an unknown password, this
might save you an evening

Lean on the learner angle: what password recovery actually does on a Catalyst,
step by step, and that the tool shows every command it sends. Point at
`ciscoyoke rescue` as read-only. Check the sub's rules first; tool posts may
need to go in a weekly thread.

## Show HN

**Title:** Show HN: Ciscoyoke – rescue old Cisco hardware from the serial console

Two or three sentences on the gap (every automation tool needs an IP; second-
hand kit has none), then the 309-tests story in one paragraph, then the link to
the write-up and the repo. HN rewards the engineering story: prompts found
per-byte so chunking can't hide them, the journal, real transcripts as
fixtures, scrubbing that failed on real data.
