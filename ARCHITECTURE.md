# Ghost Network Watch — MVP Architecture

Target: behavioral-health integrity scores for federal-marketplace (FFM) plans, published
before ACA open enrollment (2026-11-01). Grounded in `scoping/FINDINGS.md` — every design
choice below traces to a verified scoping fact.

## Principles

1. **Evidence-row-first.** Every flag is born as an evidence row
   (`flag_id, rule_version, snapshot_sha256, http_headers, raw_fragment, cross_ref`).
   The score, the drill-down UI, and the issuer-notification export are all views over that
   one table. No claim ships without its receipt.
2. **Payer's own file first.** Internal-consistency flags (E1/E2) carry the scores;
   cross-database flags (E3, NPPES) are capped at 20/100 points because either database could
   be wrong.
3. **Record-level counting, never per-address.** (CareSource lesson: per-address counting
   inflated an anomaly 2×.)
4. **Say what the file shows, never what the insurer intends.** Banned words: ghost, fake,
   phantom, fraud, padded, doesn't exist. Required verbs: lists, shows, carries, disagrees
   with, could not be retrieved.

## Pipeline (monthly full run + weekly availability ping)

```
EventBridge Scheduler (monthly / weekly)
        │
        ▼
[1] CRAWL — Fargate Spot task (files up to 200MB; not Lambda-shaped work)
    · seed: PY2026 MR-URL PUF (346 rows → 108 indexes → ~1,295 medical provider files)
    · 7 platform adapters (Centene, UHC, Oscar, Medica, Elevance, Cigna, Molina/HCSC)
      + generic CMS-schema adapter + per-host quirks registry. Crawling is polite
      (~1 req/s per host, identifying UA) and every quirk below is a documented
      observation about how a mandated public file is served, not an attempt to
      reach anything non-public. CMS guidance is explicit that these files "must
      be publicly accessible" and that access "should not depend on any specific
      user-agent or source ip address", so a host that refuses standard clients
      is itself a finding, published on that issuer's page:
        - browser UA always (UHC/Wellmark/MercyCare 403 otherwise)
        - never send Accept: application/json to IIS hosts (CHRISTUS 406)
        - follow redirects; re-resolve provider URLs from index every run (CareSource
          date-stamped dirs); retry/backoff (Elevance drops); port 8443 (wellsense);
          raw S3 hosts (AmeriHealth Caritas Next)
        - gzip + Range; prefer full census when gzipped file fits budget; ~1 req/s per host
    · write-through: s3://gnw-data-<account>/blobs/<sha256> (content-addressed,
      bucket name derived from the deploying account) + fetch manifest
      (URL, headers, timing, snapshot id) — the evidence backbone
        │
        ▼
[2] PARSE — streaming (ijson), never load whole files
    · Parquet tables partitioned by snapshot: providers, provider_addresses,
      provider_plans, plans, index_manifest
    · issuer attribution via plans[] HIOS IDs (first 5 digits) — mandatory for
      shared-index platforms (Centene/UHC/Oscar/Cigna)
        │
        ▼
[3] REFERENCE JOINS (monthly refresh, cached in S3)
    · NPPES full replacement (1.15GB zip → Parquet) + Deactivation Report xlsx
      (sole authoritative deactivation source; API cannot see deactivated NPIs)
    · NUCC taxonomy v26.1 (BH = group 10 + psychiatry codes) — authority for the
      BH classifier; string filter (bh_filter_v0.json) is candidate-screen only,
      per-platform mapping table, not a regex
    · CMS PUFs: Plan Attributes → Service Area (county FIPS per plan); landscape xlsx
      as primary on-exchange denominator; HUD-USPS ZIP↔county crosswalk
        │
        ▼
[4] FLAG ENGINE — DuckDB over Parquet (local dev == prod logic; Athena optional later)
    Feed-level:  DEAD_FEED (grade X, unauditable) · ACCESS_RESTRICTED (annotation, no penalty)
    Record-level: PLACEHOLDER_CONTACT · PLACEHOLDER_DATE (1900-01-01 ≠ staleness math)
      · STALE_ATTESTATION (two-signal: record date + HTTP Last-Modified;
        classify file-abandoned vs regenerated-not-reattested)
      · NO_IN-STATE_ADDRESS · ADDRESS_INFLATION (type- and specialty-aware;
        pair with NPPES divergence) · PHONE_CONCENTRATION (with number-attribution
        table: plan line > unattributable > provider-org central line)
      · NPI_DEACTIVATED (E3) · TAXONOMY_MISMATCH (E3)
    Structural annotations: all-providers-on-all-plans (per-plan scores suppressed,
      reported as "one undifferentiated statewide network")
        │
        ▼
[5] SCORE — cell = (StandardComponentId, county FIPS)
    · penalty = min(1.0, w_max + 0.25·Σw_other) per record (no quadruple-counting)
    · score = 0.8·county-roster integrity + 0.2·plan-file hygiene; E3 capped at 20pts
    · minimum-sample rules; letter grades + X
    · the no-threshold headline: "plan X lists N behavioral-health providers in county Y"
        │
        ▼
[6] PUBLISH
    · static site: S3 + CloudFront (same stack as soorena.io) — per-plan/county pages,
      per-issuer pages, methodology, correction channel
    · UI: Web Awesome components (wa-*), token-only styling per the wa-design-taste
      discipline; pages generated from Parquet by a build script (fragments + assemble,
      the beauty-nova-wa pattern) — no SPA framework
    · Domains owned: ghostnetworkwatch.org (primary) + .com + ghostnetwork.watch (redirects);
      contact@ghostnetworkwatch.org forwards to the operator
    · open data: Parquet/CSV + evidence-row JSONL downloads, versioned; "last refreshed
      per source" manifest published machine-readable
    · issuer pre-notification: SES to the PUF's own Tech POC emails ≥2 weeks before
      publication (right of reply; ship WITH the MVP, not after)
```

## Cost envelope

S3 (raw snapshots ~50–100GB/month compressed, lifecycle to IA/Glacier): ~$5–15/mo growing.
Fargate Spot (one monthly crawl+parse, a few hours): <$5/mo. CloudFront/SES: pennies at
launch. Total well under $30/mo initially. NPPES/PUF processing runs fine on the laptop;
cloud is for the schedule, the evidence store, and the site.

## Timeline to open enrollment (~10 weeks from 2026-08-21)

| Weeks | Milestone |
|---|---|
| 1–2 | Crawler + quirks registry + content-addressed raw store (scoping scripts are the seed) |
| 3–4 | Streaming parser → Parquet; NPPES/NUCC/PUF reference ingestion; BH classifier v1 |
| 5–6 | Flag engine + evidence rows; scoring; first full monthly snapshot |
| 7–8 | Site + open-data downloads + methodology page; second snapshot (first *decay* data) |
| 9 | Issuer pre-notification window (SES to PUF Tech POCs); corrections |
| 10 | Publish (~Oct 24–31); journalist outreach with CHRISTUS/staleness/telehealth exhibits |

## Scope guards

- Dental platforms (160 issuer rows incl. Humana's 2,995 pharmacy/dental files): **excluded**
  — halves issuer scope, cuts file count ~70%. Dental-only plans scored in a separate pool if
  ever added (Plan Attributes `DentalOnlyPlan` flag).
- State-based marketplaces (CA/NY/…): out of MVP; FFM-only (30 states). Document loudly.
- Medicare Advantage: not in MVP, but keep the schema MA-compatible — MA bulk directory files
  (same base schema) went live 2026-10-01; official CMS MA scores don't arrive until PY2029
  (REAL Health Providers Act). The 2027 expansion rides that wave.
- 50-state licensing-board scraping: explicitly deferred (v2+).

## Definition of done (MVP)

A journalist can: open a county, see every marketplace plan's BH integrity grade, click any
grade down to per-record evidence rows quoting the payer's own file (with snapshot hash and
fetch headers), download the dataset, and read the methodology — and the affected issuer got
the evidence by email two weeks before they did.
