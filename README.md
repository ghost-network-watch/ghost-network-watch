# Ghost Network Watch

A continuous public integrity audit of health insurers' federally mandated machine-readable
provider directories, focused on behavioral / mental health — per-plan, per-county integrity
scores with raw evidence attached to every flag.

**The problem.** People choose insurance plans based on provider directories that are often
fiction: unreachable numbers, retired or relocated providers, "accepting patients" flags nobody
verified. Secret-shopper studies (Senate Finance 2023; NY AG) found the large majority of
listed mental-health providers can't actually be seen. CMS's own reviews of these files
(PY2017–2021) found 29–47% fully accurate — then named no one, fined no one, and stopped.

**The wedge.** Every marketplace insurer must publish its entire provider directory as
machine-readable JSON at a public URL (45 CFR 156.230(b), updated "no less than monthly"), and
CMS publishes the index of those URLs. The proof of directory fiction sits in the insurers' own
mandated files — and the only companies equipped to audit them at scale are paid by insurers to
clean the data privately, so no one publishes the audit. This project does.

**Positioning in one line:** CMS will publish directory-accuracy scores for Medicare Advantage
starting 2029 (REAL Health Providers Act, Feb 2026). Marketplace enrollees shopping this
November get nothing — so we built it.

**What we never claim:** that a provider "doesn't exist" or an insurer is "lying." Every flag
states what the payer's own published file shows, or where it disagrees with another public
record (NPPES), with the snapshot hash, fetch headers, and verbatim record attached. Issuers
receive the evidence by email (the contact CMS's own PUF lists for them) before publication,
with a standing correction channel.

## Status

- **2026-08-21 — Scoping complete.** All 108 index URLs in the PY2026 PUF probed; 162 provider
  files (~146k records) sampled; anomalies adversarially verified; scoring rubric v0, BH filter
  v0, NPPES join, and plan/county attribution designed and verified on live data.
  See `scoping/FINDINGS.md`. First exhibits already in hand (a directory where 85% of records
  have placeholder phone `999999999`; telehealth-platform records listed at 352 addresses;
  files abandoned for 2+ years).
- **Next:** MVP build per `ARCHITECTURE.md`, targeting publication before ACA open enrollment
  (2026-11-01).

## Layout

```
ARCHITECTURE.md        MVP design + timeline
scoping/
  FINDINGS.md          what the data actually looks like (start here)
  data/                PUF, probe results, deep-dive findings JSON
  probes/              per-index probe reports (108)
  evidence/            verified exhibits, rubric, filters, citations
  *.py                 probe/sampler scripts (seeds of the real crawler)
```

## Non-goals (MVP)

Dental plans, state-based marketplaces, Medicare Advantage (2027 candidate), license-board
scraping, and any consumer plan-recommendation feature.
