<!--
DRAFT launch article. Every number and event below happened; edit the voice
freely, but check any change to a fact against the git history first.
Do not name the organisation the switch came from.
-->

# 309 tests passed. Then I plugged in a real Cisco switch.

ciscoyoke is a tool for the part of network automation that isn't automated:
getting an old Cisco box from "arrived in a cardboard box with somebody else's
password on it" to "has an IP address, so Ansible can take it from here". It
works entirely over the serial console, because a second-hand switch has no
working management IP, and every mainstream tool (Netmiko, Nornir, Ansible,
scrapli) needs one.

Before any real switch touched it, the suite stood at **309 passing tests**:
state detection over a byte stream, property tests proving prompts are found
however the serial reads split, replayed transcripts built from Cisco's
published output, a journal for interrupted recoveries, the lot.

Then I plugged in a real Catalyst 2950.

## The routers went first

The first two devices on the bench never got that far. One router was
completely silent: zero bytes at every speed, even across a power cycle. The
other, an 1812, printed a DDR memory test failure every fifteen seconds and
reset itself. Its all-zero SPD read meant no DIMM, so the failing RAM was the
128 MB soldered to the board. Both went in the bin.

That was the first lesson, before a single line of recovery ran: **the tool
said "no signal" about a router that was printing a memory failure as fast as
the console could carry it.** It knew how to identify a healthy device and how
to report silence, and it had no idea what a device failing its own power-on
test looks like.

## Then the switch

The 2950 was locked with the previous owner's console login. Everything below
happened in the first evening with it.

**1. The tool couldn't be told what it was looking at.** A locked 2950 at
`Username:` discloses nothing, so `recover access` refused, correctly, to
guess between the router and switch procedures. Its error said to "read the
label on the chassis", but there was no option to tell it what the label said.

**2. It didn't recognise the bootloader prompt it was sitting at.** After the
Mode-button step the switch was at `switch:`, and the tool timed out waiting
for `switch:`. The 2950's bootloader ends lines with `\n\r`, LF *then* CR.
Every synthetic fixture I'd written used `\r\n`. The pattern allowed a prompt
after a bare `\n`, and there was a `\r` in the way.

**3. A rename failed silently, and the tool believed it.** Password recovery on
a Catalyst renames `config.text` so the switch boots without it. The switch
already had a `config.old`, left there by whoever set it up, so
`rename config.text config.old` failed. The bootloader answered with the same
`switch:` prompt as a success, the tool took the prompt as proof, the journal
recorded the rename as done, and the switch booted straight back into the
login it was meant to bypass. My first theory (a `CONFIG_FILE` variable
pointing somewhere else) was wrong; reading the flash listing settled it.

**4. Booting into a login was reported as a timeout.** That third failure
surfaced as "expected priv_exec, observed press_return", which is true and
useless. It now says the configuration was still loaded.

**5. A log message hid a question.** Putting the configuration back, IOS asked
`Destination filename [config.text]?` and, a moment later, printed a
link-state message straight after it. The question was no longer the last
thing in the stream, so the tool waited on a prompt it could no longer see.
Console syslog is now stripped before prompt detection.

**6. It blamed the person for something that didn't happen.** One attempt
timed out with "the switch completed a normal boot, so the button was released
too early". The recording showed the switch's uptime counter ticking straight
through the wait: nobody had power-cycled it at all. It now checks for boot
output before it gives advice about the button.

After the fixes: one Mode-button press, then `ciscoyoke recover access
--no-restore` and `ciscoyoke reset`, took the switch from locked with a stranger's
configuration to a clean lab baseline, with every configuration file it deleted
read into an archive first.

## Then I tried to publish the evidence

Real sessions become test fixtures: the transcript is scrubbed, committed, and
replayed in CI forever. The scrubber had passed its synthetic tests too. On the
first real transcripts it:

- missed passwords split across two serial reads, because a real console
  delivers a line a few bytes at a time and the scrubber matched one read at a
  time;
- left a RADIUS shared key in the clear, because the line had `auth-port` and
  `acct-port` between the host and the key;
- kept a login banner giving the previous owner's name and street address,
  because no pattern can know that a banner's prose identifies somebody;
- didn't touch serial numbers or MAC addresses at all.

All four are fixed. The reset recording is still not published, and won't be:
it had to read the previous owner's configuration to archive it, and that
configuration isn't mine to publish, however well it scrubs.

## What changed because of it

- The support table in the README only gives a ● where a committed hardware
  recording backs it. The 2950 has two. Reset doesn't, for the reason above.
- Every destructive run now records itself, and a failure writes down the last
  prompt the tool didn't understand. `ciscoyoke report` packages that into one
  zip with configuration output removed, so a failure on hardware I don't own
  can be fixed from what it left behind.
- Knowledge per switch family now lives in one table, labelled by where it came
  from. The 2960 entries come from Cisco's documentation and are marked as
  such, because this post is what happens when you trust documentation-derived
  code on real hardware.

309 tests told me the code did what I designed it to do. One switch told me what
the design had missed. If you have an old Catalyst or ISR in a cupboard, I'd
love a `ciscoyoke report` from it:

    pipx install git+https://github.com/Ryan-Clinton/ciscoyoke
    ciscoyoke rescue COM4

https://github.com/Ryan-Clinton/ciscoyoke
