# Scoping Findings — Ghost Network Watch

Date: 2026-08-21. Method: exhaustive probe of all 108 unique index URLs in the CMS PY2026
Machine-Readable URL PUF (346 issuer rows, 30 FFM states), Range-limited sampling of 162
provider files (~146k records), then 10 verification/design agents with fresh fetches and
web research. Evidence exhibits in `evidence/`; full agent findings in
`data/deepdive_findings.json`; probe reports in `probes/`.

## Verdict

**The pipeline is buildable, the lane is empty, and the first exhibits are already in hand.**
Nothing found blocks the MVP; several findings materially reshape it (see "Design corrections").

## Feasibility facts

- 106/108 indexes reachable; **100% of reachable indexes are valid JSON conforming to the CMS
  index schema**. 4,337 provider file URLs total.
- **NPI present on 100% of sampled records, 100% Luhn-valid** → NPPES join is clean.
- **46% of issuer rows (160/346) are dental-only** and out of BH scope. In-scope: 186 medical
  rows, ~1,295 provider files, ≥29GB (files up to ~200MB; streaming parser mandatory).
- **7 platform adapters cover 102 of 186 medical rows** (Centene 24, UHC 20, Oscar 15, Medica 9,
  Elevance 9, Cigna 9, Molina 6; +HCSC 3) + one generic CMS-schema adapter with a per-host
  quirks registry for the long tail. See `evidence/hosting_platform_landscape.json`.
- Mystery CloudFront host `d3ul0st9g52g6o.cloudfront.net` = **Oscar Health** (identified via PUF
  Tech POC email). BH-heavy (~47–54% of records), fresh data.
- Full-file censuses are often possible within politeness budget: gzip + Range transfers
  CareSource's 82MB WI file complete in <8MB.

## Headline integrity exhibits (verified with fresh fetches)

1. **CHRISTUS Health Plan (TX 66252, LA 98780) — the flagship exhibit.** Its only current
   provider file (108MB): **84.6% of records have phone `999999999`, address `"null"`, state
   `UN`, zip `99999`**; 100% of 8,203 sampled records dated `1900-01-01`; zero fully-clean
   records. **95.4% of behavioral-health records are uncontactable while 89.6% of records are
   marked "accepting" new patients.** Second file untouched since 2024-08 (~2 years).
   → `evidence/christus-hsp-provider-*`
2. **Telehealth-platform address inflation is a BH-specific pattern (Medica MO).** The four
   largest records are telehealth-platform NPIs: a Headway-affiliated PC at **352 Missouri
   addresses sharing one Manhattan phone**, Alma at 161+51, Headway MI at 86, SonderMind at 51 —
   NPPES places each at a single out-of-state corporate address.
   → `evidence/address-inflation/`
3. **BCBS Wyoming vision file:** worst record = an optometrist at **341 addresses** (NPPES: one
   Michigan office); **~99.9% of sampled records in this "Wyoming" file have no Wyoming
   address**.
4. **CareSource WI:** 9.0% of the entire file (1,792/19,956 records) has **zero Wisconsin
   service addresses**; every record is attached to **all 32 plan/tier combinations**, so the
   issuer publishes one undifferentiated statewide network (per-plan scores meaningless there).
   → `evidence/caresource-8663892727-minuteclinic-exhibit.json`
5. **Staleness:** 15 stale indexes covering 27 issuer IDs in 15 states, split by HTTP
   Last-Modified into **file-abandoned** (Aspirus WI: newest attestation 807 days old;
   CHRISTUS; DentaQuest ×8 IDs; Antidote ×2) vs **regenerated-but-not-reattested** (Kaiser
   HI/OR, Wellmark SD, insuringsmiles). CMS requirement chain verified: 45 CFR 156.230(b) →
   CMS-10558 → Final 2017 Letter to Issuers: update "**no less than monthly**".
   → `evidence/staleness_exhibit.{json,csv}`
6. **Access failures:** BCBSNC index hard-dead (404). UHC, Wellmark, Mercy Care serve **403 to
   non-browser User-Agents** on legally mandated "machine-readable" files. SummaCare's domain
   was DNS-dead at first probe and recovered days later — availability itself fluctuates, which
   is the argument for *continuous* monitoring.
7. **Providence:** sampled BH file shows `accepting: "not accepting"` on 100% of records.

## Design corrections (things we'd have gotten wrong)

- **The "31% accepting-unknown" stat was a schema artifact.** The missing field tracks
  FACILITY/GROUP records almost exactly; among 97,272 INDIVIDUAL records it's ~0.3%. The naive
  claim is now the rubric's honesty-standard example — never publish it.
- **The CareSource "call-center phone" story was wrong.** 866-389-2727 is CVS MinuteClinic's
  national booking line (matches the org's own NPPES record), not CareSource's call center, and
  zero BH records were affected. Defensible replacements: phone-concentration *with number
  attribution*, and the zero-in-state-address flag. Metrics must count at **record level, never
  per address row** (per-address counting inflated the anomaly ~2×).
- **`last_updated_on` is usually a file-generation timestamp**: 75% of files (n>50) stamp every
  record identically. Staleness is one-directional evidence; "fresh" must never be presented as
  "verified".
- **A single BH regex cannot classify specialties.** 536 candidate strings observed; Cigna uses
  conditions-treated vocabulary ("Anxiety Issues"), TruAssure uses 2-letter codes, MT CO-OP has
  empty specialties, "developMENTAL" false-positives. v0 filter (433 include / 73 exclude / 30
  ambiguous) in `evidence/bh_filter_v0.json`; **NPPES taxonomy (NUCC-coded) is the authority**,
  string filter is only a candidate screen. NUCC v26.1 archived in `evidence/`.
- **NPPES deactivations are invisible to the API and removed from the main file.** The monthly
  Deactivation Report xlsx (351,912 NPIs; NPI + date only) is the sole authoritative source.
  Monthly full file: 1.15GB zip / 11.6GB CSV — laptop-tractable.
  → `evidence/nppes-source-verification-2026-08-21.json`
- **Plan/county attribution verified end-to-end on live PY2026 data:** provider `plans[]` HIOS
  Standard Component ID → Plan Attributes PUF (exactly 1 ServiceAreaId each) → Service Area PUF
  county FIPS list; matches the QHP landscape file exactly. `plans.json` carries **zero
  geography** — PUFs are mandatory. Provider→county via HUD-USPS ZIP-county crosswalk.
  Score cell = (StandardComponentId, county FIPS).
  → `evidence/plan_county_attribution_verification.json`, `evidence/puf_urls_py2026.json`

## Scoring rubric v0

10 metrics (2 feed-level, 8 record-level), each grounded in measured prevalence; per-record
penalty de-duplication; score = 0.8×county-roster integrity + 0.2×plan-file hygiene; NPPES
(cross-database) deductions capped at 20 points; E1/E2/E3 evidence-strength tiers; grade **X
(unauditable)** distinct from F; defamation-safe phrasing templates per metric; banned words
(ghost/fake/phantom/fraud); issuer pre-notification via the PUF's own Tech POC emails before
publication. Full text: `evidence/scoring_rubric_v0.md`.

## Prior art & positioning (verified 2026-08-21)

The continuous-public-integrity-audit lane is **empty**. HealthPorta/Ideon/HiLabs = commercial
aggregation, no published quality metrics (treat as validators, not competitors). Academic
audits (Zhu 2022 "phantom networks", Butala JAMA 2023, Senate Finance 2023) = one-off snapshots.
NY AG enforcement is episodic (EmblemHealth $2.5M settlement, Feb 2026; 86% ghost rate). CMS's
own QHP file reviews (PY2017–2021: **29–47% fully accurate**) named no issuers, imposed no
penalties, and have no public successor.

**The news hook:** the REAL Health Providers Act (signed 2026-02-03) mandates CMS-published
plan-level directory-accuracy scores — but only for **Medicare Advantage, starting PY2029**.
Nothing equivalent exists for ACA marketplace plans. GNW's one-liner: *"CMS will score Medicare
Advantage directories in 2029. Marketplace enrollees shopping this November get nothing — so we
built it."* MA bulk directory files go live 2026-10-01 (same base schema) → natural 2027
expansion. Citations: `evidence/prior_art_citations.json`.
