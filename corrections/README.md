# Corrections

The site promises that disputes are published alongside the findings they
dispute. This directory is how that promise is kept.

`corrections.json` is a list of correction records. The site generator reads it
and renders `/corrections/` plus a per-issuer section on each issuer page. It is
maintained by hand, on purpose: a dispute arrives by email or on the public
tracker, and a human transcribes it here.

Keeping it in git rather than a database is deliberate. Every edit to a
published statement is visible in `git log -p corrections/corrections.json`, so
"published verbatim" is a checkable claim rather than an assurance. Do not
rewrite history in this file. If a statement needs to change, add a follow-up
record.

## Adding a correction

Append an object with these fields. Everything except `response` and the
resolution fields is required.

| Field | What it holds |
|---|---|
| `id` | Short kebab-case slug, unique and stable. Used as the anchor. |
| `received` | ISO date the dispute arrived. |
| `source` | Where it came from: `tracker#12`, `email`, or similar. |
| `issuer_id` | HIOS issuer ID it concerns, or `null` for a site-wide dispute. |
| `disputes` | What it disputes, in prose. Name the metric and NPI when known. |
| `role_claimed` | `insurer`, `provider`, or `public`. |
| `issuer_verified` | `true` only if it arrived from the CMS Tech POC address on file for that issuer. Self-assertion is not verification. |
| `statement` | The filer's words, verbatim. Do not paraphrase or tidy. |
| `redacted` | `false`, or a short note of what was removed and why. |
| `response` | Our reply, or `null` while we are still working on it. |
| `status` | `open`, `accepted`, `declined`, or `resolved_in_crawl`. |
| `resolved_snapshot` | Snapshot where the finding stopped reproducing, else `null`. |

`status` meanings:

- **open** — received, no response yet.
- **accepted** — we agree; the finding was wrong and is withdrawn or amended.
- **declined** — we disagree and explain why. Both sides stay published.
- **resolved_in_crawl** — the insurer changed the underlying file and a later
  crawl no longer reproduces the finding. This is not an admission by either
  side, only an observation about the file.

## Redaction

The one carve-out on verbatim publication is material we will not host:
personal or health information, credentials, or abuse. When that happens, remove
it from `statement`, and say so in `redacted`. Never edit silently.

## Example

```json
[
  {
    "id": "example-issuer-disputes-placeholder-phone",
    "received": "2026-11-04",
    "source": "email",
    "issuer_id": "12345",
    "disputes": "M3_PLACEHOLDER_VALUE on 41 records in the TX file",
    "role_claimed": "insurer",
    "issuer_verified": true,
    "statement": "The numbers flagged as placeholders are our central intake line...",
    "redacted": false,
    "response": "The flag reads a literal 999999999 in the published file...",
    "status": "declined",
    "resolved_snapshot": null
  }
]
```
