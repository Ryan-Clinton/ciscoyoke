# Security

ciscoyoke is not a network service, but it handles things that are sensitive:
device credentials, configuration backups and console recordings that may hold
a previous owner's secrets.

## Never post these in a public issue or pull request

- credentials, in any form: passwords, enable secrets, SNMP communities, hashes
- raw transcripts (`.ytx`): only scrubbed `.ytx.pub` files are shareable
- configuration backups or `archive` bundles
- private keys and key blocks
- anything that identifies a real network: public addresses, hostnames, site
  names

`ciscoyoke report` produces a scrubbed zip with configuration output removed.
Read the transcript inside before attaching it: automated scrubbing cannot prove
a recording holds nothing sensitive.

If you have already posted something you should not have, say so in the issue
and it will be removed. Rotate the credential anyway; assume it was seen.

## Reporting a vulnerability

Report privately through
[GitHub's private vulnerability reporting](https://github.com/Ryan-Clinton/ciscoyoke/security/advisories/new),
not in a public issue. Things worth reporting this way:

- a secret that survives `transcript scrub` or reaches a `report` zip
- a secret written to a log, journal, transcript or support bundle in plaintext
- a way to make ciscoyoke send destructive commands without `--confirm`
- the embedded TFTP server serving or accepting anything beyond the one image

Expect a reply within a week. This is an alpha maintained by one person; only
the latest release is supported.

What the tool defends against, and what it does not, is in
[docs/THREAT-MODEL.md](docs/THREAT-MODEL.md).
