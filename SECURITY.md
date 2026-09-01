# Security and privacy reporting

## Reporting a vulnerability

Email **contact@ghostnetworkwatch.org**. Please do not open a public issue for a
vulnerability, since the tracker is public and indexed.

Useful things to include: what you found, how to reproduce it, and what an
attacker could do with it. You will get an acknowledgement, and a fix or an
explanation of why something is working as intended. This is a one-person
public-interest project, not a company with a response team, so expect days
rather than hours.

There is no bug bounty. Reports made in good faith are welcome regardless.

## Reporting a privacy problem

This project reads insurers' machine-readable provider directory files, which
insurers publish under 45 CFR 156.230(c). Those files describe real clinicians.
The project's rule is that published outputs identify providers by their
National Provider Identifier, a public federal identifier, and not by name.

If you find published output that breaks that rule, or that exposes personal
information about a patient, member, or provider, email the address above and
say so plainly. That takes priority over everything else in the queue.

Specifically in scope:

- A published evidence row, page, or export carrying a person's name, home
  address, personal phone number, or any health information.
- Anything in this repository's history that carries the same.
- A correction filed on the public tracker that contains personal details. We
  redact those and note in the thread that we did.

If you are a listed clinician and your directory entry is wrong, note that we
did not create that entry. It is what your payer published about you, and the
fix has to come from the payer. We are happy to point you at the exact file,
timestamp, and record so you can take it to them, and to record your account
alongside the finding.

## Scope

In scope: this repository, the pipeline it contains, and the site published at
ghostnetworkwatch.org.

Out of scope: the insurers' own websites and directory files. Those are third
party systems. Please do not test them, and do not report findings about their
infrastructure here.
