# Ghost Network Watch — Directory Integrity Score, Rubric v0

**Status:** draft v0 for MVP build · **Date:** 2026-08-21 · **Scoring year:** PY2026 QHP machine-readable files
**Scope:** CMS-mandated machine-readable provider directories (QHP provider JSON), 30 FFM states, 346 issuer rows, 108 unique index URLs.
**Grounding:** every metric below was observed in scoping (108 index probes; 162 provider files sampled; 145,948 records analyzed; 4 full-file CareSource parses; NPPES source verification; address-inflation and staleness exhibits). Prevalence figures cite those artifacts.

---

## 0. What this score is — and is not

The Directory Integrity Score measures **whether an insurer's federally mandated, self-published provider file is internally consistent, current, and usable as published**. It is computed only from:

1. **The payer's own published bytes** (Tier A / self-published evidence), and
2. **Disagreement between the payer's file and federal public registries** (Tier B / cross-database evidence — currently NPPES).

It does **not** measure, and must never be presented as measuring:

- whether any listed provider exists, practices, or accepts patients in reality;
- whether any insurer intended to mislead;
- clinical quality or network adequacy in the regulatory sense.

Every flag is a statement of the form *"the payer's own published file shows X"* or *"the payer's file disagrees with public record Y."* Every plan-county score decomposes into per-record evidence rows (§5). This constraint came out of adversarial review and is non-negotiable: **flags prove database disagreement, not ghost-ness.**

---

## 1. Metrics (10)

Two levels: **feed-level** (per issuer/index; gates and annotations) and **record-level** (per provider record; these aggregate into scores). Each metric carries an evidence-strength label (§4): **E1** self-published & deterministic, **E2** self-published & threshold-based, **E3** cross-database disagreement.

### Feed-level

#### M1 · DEAD_FEED — gate, E1

**Definition.** The CMS-submitted index URL, or ≥50% of the provider file URLs it lists, return HTTP ≥400 / DNS failure / unparseable JSON on ≥2 crawl attempts ≥72h apart, using the standard browser User-Agent.
**Observed.** 2/108 indexes (1.9%): BCBSNC index 404; myplancentral DNS-dead. All 106 reachable indexes were valid JSON conforming to the CMS index schema (`index_probe_results.jsonl`).
**Effect.** All plan-counties of the issuer receive grade **X — Unauditable** (no numeric score; see §3.5). This is deliberately not an F: an F asserts bad content, X asserts absent content.

#### M2 · ACCESS_RESTRICTED — annotation, E1

**Definition.** Feed serves the content only under a browser User-Agent (403/challenge to non-browser fetchers), or requires JS challenges (observed: one Cloudflare challenge on Medica during exhibit work). Recorded when the same URL yields 403 with a plain client and 200 with the browser UA on the same day.
**Observed.** 3 issuer feeds (UHC, Wellmark, MercyCare) 403 non-browser UAs; NPPES's own download page does the same.
**Effect.** No score deduction in v0 (content, once fetched, is complete). Published as a transparency badge: "This machine-readable file was not retrievable with standard automated clients on [date]." Rationale: the files exist to be machine-read; blocking machines is a fact worth publishing, but scoring it would import an access-policy judgment into a data-integrity score.

### Record-level — Tier A (self-published)

#### M3 · PLACEHOLDER_VALUE — E1

**Definition.** Any of the following literal sub-flags on a record (sub-code recorded per evidence row):

- `PHONE`: phone digits are all-same-digit (`9999999999`, `0000000000`, `999999999`, `0`), empty string, or <10 digits after stripping punctuation.
- `ZIP`: zip `99999`, `00000`, or non-5/9-digit.
- `ADDRESS`: street address empty, literal `null`/`NULL`/`N/A`, or missing on a record whose type requires it.
- `DATE`: `last_updated_on` earlier than 2014-01-01 or later than the fetch date. (The CMS MR-directory mandate began PY2016; any earlier "attestation" is a sentinel, not information.)

**Observed.** PHONE: 845/1,000 sampled CHRISTUS LA/TX records carry phone `999999999` (9 digits); 100% of both sampled Priority Health MI files carry empty-string phone (822/822 and 875/875); Guardian dental ~2% `0000000000`; scattered `0` and `9999999999` elsewhere. ≈1.8% of the 145,948-record sample overall, heavily concentrated in ~5 of 108 feeds. DATE: 1,000 records at `1900-01-01` (CHRISTUS, same file as the 999-phones), 146 at `1990-01-01` (Aspirus WI); 1.3% of all sampled records dated before 2014.
**Weight rationale.** A record with placeholder contact data is unusable *as published*, by the payer's own bytes — the strongest, most defensible flag class we have.

#### M4 · STALE_ATTESTATION — E2

**Definition.** `last_updated_on` more than **180 days** before fetch date (graded: ≥180d = stale; ≥365d = severely stale). Excludes records already flagged PLACEHOLDER_VALUE:DATE (no double count).
**Observed.** ≈23.5% of 145,948 sampled records dated 2025 or earlier (all >180d as of 2026-08-21). At file level: 24/162 files (15%) have *no* record newer than 180 days; 6/162 (4%) none newer than a year. Staleness exhibit's worst: Aspirus 807d, CHRISTUS 742d, Kaiser HI 381d.
**Two mandatory caveats, published with the metric:**

1. **Bulk-stamping.** In 122/162 sampled files (75%), every sampled record shares one identical `last_updated_on` — the field is a file-generation timestamp, not a per-record attestation. Therefore staleness is **one-directional evidence**: an old date is meaningful; a fresh date is *not* evidence the content was verified. Never present a fresh file as "verified."
2. **Sub-classification** (from `staleness_exhibit.csv`), attached at file level: `file-abandoned` (HTTP Last-Modified itself >180d — e.g., CHRISTUS, Aspirus, DentaQuest, Antidote OH/AZ), `regenerated-not-reattested` (fresh Last-Modified, stale attestations — e.g., Kaiser HI/OR, Wellmark SD, Insuring Smiles), `no-freshness-headers` (indeterminate).

#### M5 · CALL_CENTER_ONLY — E2 (with E3 enhancer)

**Definition.** Every phone on every address of the record equals a "concentration number": a phone appearing on ≥1% of records **or** ≥50 records in the same provider file (whichever is larger). Sub-flag `NPPES_DIVERGENT_PHONE` (E3) when NPPES lists a different practice phone for that NPI.
**Observed.** Full-file CareSource parse for phone 866-389-2727 (a CVS/MinuteClinic corporate line): records reachable *only* via it = **WI 3.29%** (657/19,956), IN 0.25%, OH 0.19%, WV 0.01%. NPPES spot-checks on 5 affected NPIs: all resolve to different direct phones and different cities (e.g., NPI 1346594959, payer file phone 866-389-2727 only, NPPES phone 770-277-5996, Loganville GA). Market-wide: in 11/156 sampled files (7%), a single number covers ≥25% of sampled records. Honest negative: in the CareSource behavioral-health subsets, **0%** of records were call-center-only — this metric's observed burden falls mostly outside BH there.
**Note.** Duplicate-phone counts alone are not the flag (large clinics legitimately share lines — e.g., 149/1,000 Moda records share one clinic number). The flag requires the record to have *no other* number.

#### M6 · ADDRESS_INFLATION — E2 (with E3 enhancer)

**Definition.** Record lists more than **N unique street addresses**, tiered by type: INDIVIDUAL >10 (strong at >25); GROUP/FACILITY >25 (strong at >100). Specialty exclusion list applied to INDIVIDUALs with legitimate multi-site patterns (radiology, pathology, anesthesiology, EM/hospitalist — from the address-inflation exhibit's counter-example: a radiologist plausibly covering 79 imaging sites). Sub-flag `NPPES_LOCATION_DIVERGENCE` (E3): directory address count vs NPPES practice-location record.
**Observed.** 89/162 files (55%) contain a record with >10 addresses; 31/162 (19%) with >50; 10/162 (6%) with >100; max seen 341 (an individual optometrist at 341 retail locations in 6 states, WY vision file) and 352 (Medica MO GROUP "Great Lakes Behavioral Health Services PC" — 352 Missouri addresses, **all sharing one Manhattan phone number**; NPPES lists one out-of-state corporate address). WY vision file: 16.2% of records >10 addresses. Mechanisms documented: vision-network fan-out, telehealth-platform NPIs (Headway/Alma/SonderMind — behavioral health specifically), umbrella IPA/PPO NPIs.
**Weight rationale.** Inflation dilutes the directory's usefulness and inflates apparent network size, but multi-site practice is sometimes real: mid weight, specialty-aware, and the E3 enhancer carries the citable contrast.

#### M7 · OUT_OF_AREA_LISTING — E2

**Definition.** Record is attached (via `plans[].plan_id` → StandardComponentId) to plan P, but has **no address in any county of P's service area** (per the CMS Service Area PUF) **nor in any county adjacent to that service area** (border-practice buffer). Evaluated per plan, not per file-state label, because one file may legitimately serve many states (Guardian's file covers 21 states).
**Observed (file-level proxies pending per-plan build).** WY issuer 11269 vision file: ~99.9% of sampled records (head *and* tail of file) contain no Wyoming address; Alliant TN: only 20.4% of sampled records have a TN address; Medica MO: 76% have an MO address; CareSource WI: 91% have a WI address (the 842-record only-call-center cohort's address footprint is GA/IN/OH/NC — including six records literally named "MinuteClinic Diagnostic of Indiana LLC" listed with WI marketplace plan IDs).
**Weight rationale.** A listing that cannot serve the plan's geography inflates the county roster without adding access. High weight, but E2 because the service-area buffer is a chosen threshold.

#### M8 · ACCEPTING_UNKNOWN — E1

**Definition.** `accepting` missing, empty, or literal `unknown` on records of type INDIVIDUAL only. (CMS schema requires the field for individual providers; FACILITY/GROUP records legitimately omit it.)
**Observed.** 0.3% of 97,272 sampled INDIVIDUAL records (`unknown` 288, blank 33, absent 2). **Published confound note:** the raw market-wide figure of 31.4% missing `accepting` is a schema artifact — it is almost exactly the facility+group record count (45,893 ≈ 48,676). Scoping nearly shipped a wrong 31% headline; the rubric memorializes the correction as an example of the honesty standard. `not accepting` (5.6%) is *not* an integrity flag — a payer honestly reporting closed panels is doing exactly what we want — but it is reported separately as a BH-access statistic.
**Weight rationale.** Low weight; low observed prevalence; kept because it is free to compute, CMS-required, and its absence is deterministic.

### Record-level — Tier B (cross-database)

#### M9 · NPI_REGISTRY_STATUS — E1 (invalid/missing) / E3 (deactivated)

**Definition.** Sub-flags:
- `MISSING` / `LUHN_INVALID` (E1): NPI absent or fails the ISO-7812 Luhn check with 80840 prefix. Self-published, deterministic.
- `DEACTIVATED` (E3): NPI appears in the NPPES cumulative Deactivated-NPI report (351,912 NPIs as of the 2026-08-10 report) **and** is absent from the NPPES monthly full file (~9.7M active records; readme confirms deactivated NPIs are excluded from it). Both conditions required.
**Observed.** MISSING: 2/145,948; LUHN_INVALID: 9/145,948 (0.008% combined — NPI hygiene is excellent market-wide). DEACTIVATED prevalence: **TBD** — requires the full join; feasibility verified end-to-end (`nppes-source-verification-2026-08-21.json`; V2 files only, filenames change each cycle, deactivation *reasons* are not public).
**Mandatory framing.** Deactivation has many causes (retirement, death, entity consolidation, voluntary deactivation). The flag asserts only that a currently-published directory entry carries an identifier the federal registry marks deactivated as of a dated report.

#### M10 · TAXONOMY_MISMATCH — E3

**Definition.** The record's directory `specialty` maps, via a curated crosswalk, to a taxonomy *group* disjoint from every NPPES taxonomy on the NPI. Fires only on **group-level** disagreement (e.g., directory says "Psychiatry," NPPES says "Physical Therapist") — never on sibling-level differences (LPC vs LMFT). Crosswalk must handle free-text vocabulary: 88 unique specialty strings in a single sampled file, HTML entities (`OBSTETRICS &amp; GYNECOLOGY`), and payer-specific labels ("BEHAVIORAL HEALTH THERAPIST").
**Observed.** Prevalence TBD. The 5 NPPES spot-checks run during scoping all *matched* on taxonomy while mismatching on phone/location — expect low base rates; that is fine, this metric mostly protects the BH-scoping step (below) from misclassification.
**BH scoping dependency.** The BH-scoped score (§3.1) uses this same crosswalk as a whitelist. The broad scoping regex matched 14.1% of records but includes PT/OT/speech false positives; the whitelist must be taxonomy-grounded, not regex-grounded.

---

## 2. Metric summary table

| # | Metric | Level | Tier | Evidence | Weight (w) | Observed prevalence (scoping) |
|---|--------|-------|------|----------|-----------|-------------------------------|
| M1 | DEAD_FEED | feed | A | E1 | gate → X | 2/108 indexes |
| M2 | ACCESS_RESTRICTED | feed | A | E1 | annotation | 3/108 feeds |
| M3 | PLACEHOLDER_VALUE (phone/zip/addr/date) | record | A | E1 | **1.0** (0.7 for DATE) | ~1.8% phones; 0.8% epoch dates; concentrated in ~5 feeds |
| M4 | STALE_ATTESTATION | record | A | E2 | 0.5 (>180d) / 0.8 (>365d) | ~23.5% of sampled records; 15% of files wholly stale |
| M5 | CALL_CENTER_ONLY | record | A(+B) | E2(+E3) | 0.6 (+0.2 enhancer) | 0.01–3.3% per full-file state; 7% of files have a ≥25% phone |
| M6 | ADDRESS_INFLATION | record | A(+B) | E2(+E3) | 0.4 (0.6 strong tier) | worst-record >10 addrs in 55% of files; max 341/352 |
| M7 | OUT_OF_AREA_LISTING | record | A | E2 | 0.8 | 9%–99.9% no-in-state-address in audited files (per-plan TBD) |
| M8 | ACCEPTING_UNKNOWN | record | A | E1 | 0.3 | 0.3% of INDIVIDUAL records |
| M9 | NPI_REGISTRY_STATUS | record | A/B | E1/E3 | 1.0 invalid / 0.7 deactivated | 0.008% invalid; deactivated TBD |
| M10 | TAXONOMY_MISMATCH | record | B | E3 | 0.5 | TBD (spot checks: 0/5) |

Weights are v0 priors, chosen on one axis — *how completely the defect destroys the record's usability as published* — and are explicitly provisional. §3.4 requires publishing a uniform-weight robustness score alongside.

---

## 3. Aggregation: plan × county → 0–100 → letter grade

### 3.1 Scoring unit and roster construction

- **Unit:** StandardComponentId (SCID, `^\d{5}[A-Z]{2}\d{7}$`) × county FIPS. Verified end-to-end: provider `plans[].plan_id` (variant suffix `-\d{2}` stripped) → SCID → exactly one ServiceAreaId in the Plan Attributes PUF (0 exceptions in 5,144 SCIDs) → county list in the Service Area PUF (`plan_county_attribution_verification.json`). Partial-county rows (23, MI/OR/TX) resolved by their explicit zip lists.
- **County roster:** records attached to the SCID with ≥1 address whose zip maps to the county (HUD-USPS ZIP-county crosswalk; a record can appear in several county rosters).
- **Two scopes, always published side by side:** **BH-scoped score** (primary; records whose crosswalked taxonomy group is behavioral/mental health — psychiatry, psychology, counseling/therapy, psychiatric NP/PA, SUD treatment; explicitly excluding PT/OT/speech) and **all-provider score** (context).

### 3.2 Record penalty (de-duplication rule)

A record's flags do not stack linearly — one broken record must not be counted four times:

```
penalty(r) = min(1.0, w_max(r) + 0.25 × Σ w_other(r))
```

where `w_max` is the record's highest-weight flag and `w_other` the rest. A record with placeholder phone (1.0) AND 1900 date AND stale flag caps at 1.0; a record that is merely stale scores 0.5.

### 3.3 Component scores

- **Component A — county-roster integrity (80%):** `A = 100 × (1 − mean penalty over county roster records)`, computed per scope (BH / all).
- **Component B — plan-file hygiene (20%):** `B = 100 × (1 − w7 × out_of_area_rate(plan))` — the plan-wide share of records attached to the SCID with no service-area(+adjacent) address. Kept separate because out-of-area records never enter any county roster (they self-exclude), yet they are the single most dramatic defect observed (WY vision file ≈99.9%); without Component B they would be invisible.
- **Composite:** `Score = 0.8A + 0.2B`, clamped [0, 100].

### 3.4 Tier-B cap and robustness score

Cross-database flags (M9-deactivated, M10, E3 enhancers) may deduct **at most 20 points** of the composite. Compute `Score_TierA_only`; final `Score = max(Score, Score_TierA_only − 20)`. Rationale from adversarial review: internal-consistency checks are the defensible core; NPPES itself is provider-self-reported and lags reality, so registry disagreement is capped until the correction channel (§4) has operated for a full cycle. Additionally publish `Score_uniform` (all weights = 0.6) as a weight-sensitivity check; if letter grades differ between weighted and uniform, the plan page must show both.

### 3.5 Minimum-sample rules and grade bands

| County-roster n (per scope) | Treatment |
|---|---|
| n ≥ 30 | Numeric score + grade + Wilson 95% interval per flag rate |
| 10 ≤ n < 30 | Grade with "low sample" badge; suppress numeric score if any flag-rate interval is wider than 25 points |
| n < 10 | **Not scored.** Publish instead the roster fact itself: "Plan P's file lists only n BH provider records with an address in County C" — for the BH scope this thin-roster fact is a first-class finding, not a data gap |

Bands: **A** 90–100 · **B** 80–89 · **C** 70–79 · **D** 55–69 · **F** <55 · **X** unauditable (M1 gate / unparseable). Bands are ordinal transparency tiers calibrated to nothing external in v0, and the methodology page says exactly that. "Worst directories" rankings must exclude X and low-sample cohorts from the ranked list (they get their own list).

---

## 4. Honest-uncertainty layer

### 4.1 Evidence-strength labels (shown on every flag, every page)

- **E1 — Self-published, deterministic.** Reproducible from the payer's archived bytes alone; no thresholds, no external data. (M1, M3, M8, M9-invalid.)
- **E2 — Self-published, threshold-based.** Same evidence base, but a chosen cutoff (180 days, 10 addresses, ≥50-record phone concentration, adjacency buffer). Methodology page publishes each threshold *and the flag rate at alternate thresholds* (90/180/365d; 5/10/25 addresses) so a reader can see how much the number moves.
- **E3 — Cross-database disagreement.** The payer's file and a dated federal registry snapshot disagree. Either database could be the stale one. Never presented without the registry snapshot date and the multiple-innocent-causes note.

### 4.2 Known biases, stated in the methodology

1. **Sampling bias (until full parses ship):** scoping stats come from Range-limited head-of-file samples (~1,000 records/file); files may be sorted. (The WY exhibit tail-sampled to control for this; the MVP pipeline should stream-parse full files.)
2. **Bulk-stamped attestations:** 75% of files stamp one date on every record → freshness is never exculpatory (§M4).
3. **Facility/group schema artifacts:** fields "missing" on non-individual records (name, specialty, accepting) are schema-conformant, not defects. The 31%-accepting artifact is the canonical example.
4. **Aggregator effects:** one platform hosts many issuers (bestlife, Delta Dental, Centene, formularynavigator) — a platform bug will correlate scores across "different" issuers; issuer pages must disclose the hosting platform (`hosting_platform_landscape.json`).
5. **NPPES is not ground truth:** it is another self-reported database with its own lag; that is exactly why Tier B is capped.

### 4.3 Methodology and correction-channel requirements (publication gate)

No score page ships unless all of these exist:

1. **Versioned public methodology** (this rubric, its thresholds, weights, and changelog) linked from every score.
2. **Evidence rows** (§5) downloadable per plan-county.
3. **Snapshot archive:** every fetched file stored with URL, UTC timestamp, response headers, and SHA-256; claims cite the snapshot, not the live URL.
4. **Issuer pre-notification:** ≥14 days before first publication, email each issuer's Tech POC (the MR PUF ships POC emails — the correction channel is built into the source data) with their machine-readable flag export.
5. **Correction workflow:** a public form + email; issuer responses published verbatim alongside the flags they dispute; flags cleared by a subsequent crawl are marked "resolved on [date]" (history retained, not deleted).
6. **Re-crawl cadence:** monthly, aligned to NPPES monthly releases; scores carry their as-of date; stale score pages (>60 days) auto-badge "awaiting re-crawl."

---

## 5. Evidence-row schema (every flag decomposes to this)

```json
{
  "flag_id": "GNW-2026-000001",
  "rule": "M3.PHONE", "rule_version": "v0",
  "tier": "A", "evidence_strength": "E1",
  "issuer_id": "98780", "plan_id": "98780TX...", "county_fips": "48029",
  "source_url": "https://chppayment.christushealth.org/workfiles/json/HSP_PROVIDER.json",
  "fetched_at_utc": "2026-08-21T05:01:00Z",
  "snapshot_sha256": "…", "http_headers": {"last-modified": "…", "etag": "…"},
  "record_locator": {"npi": "…", "record_index": 412},
  "raw_fragment": {"addresses": [{"phone": "999999999", "...": "as published"}]},
  "threshold_params": null,
  "cross_ref": null
}
```

For E3 flags, `cross_ref` holds the registry citation: `{"source": "NPPES Deactivated NPI Report", "report_date": "2026-08-10", "value": "deactivation_date=2024-03-11"}`.

Presentation rule for named individuals: aggregate views show counts only; evidence rows quote the payer's file verbatim (NPI + name as published) under a standing banner — *"Provider details are quoted from the insurer's published directory file. No claim is made about the provider; listed clinicians are often unaware of how they appear in payer directories."*

---

## 6. Defamation-safe language templates

**Global rules.** Attribute every claim to a dated file or dated registry snapshot. Banned as assertions about any specific record/insurer: "ghost provider," "fake," "phantom," "fraud," "lying," "doesn't exist," "padded" ("ghost networks" is permitted only as the name of the general research topic, with citation). Required verbs: "lists," "shows," "carries," "disagrees with," "could not be retrieved." Never "verified" for fresh attestation dates (§M4 caveat 1).

- **M1 DEAD_FEED:** "The machine-readable directory URL that [Issuer] submitted to CMS for plan year 2026 ([URL]) could not be retrieved on [dates] (HTTP [code]/DNS failure). We could not audit this issuer's directory; its plans are graded X (unauditable), not scored."
- **M2 ACCESS_RESTRICTED:** "[Issuer]'s directory file was served only to requests presenting a web-browser User-Agent on [date]; standard automated clients received HTTP 403. The data below was retrieved with a browser User-Agent and appears complete."
- **M3 PLACEHOLDER_VALUE:** "As of [fetch date], [N] of [M] records in [Issuer]'s published provider file carry the phone value '[999999999]' / an empty phone field / a `last_updated_on` of [1900-01-01]. These are the values as published by the issuer; the file itself provides no working contact information for these listings."
- **M4 STALE_ATTESTATION:** "[N] of [M] records in [plan]'s file carry a `last_updated_on` older than 180 days as of [date] (oldest: [date]). This measures the file's own attestation dates. A recent date is not evidence a record was verified: in this file, [all sampled records share a single date], consistent with a bulk-generated timestamp."
- **M5 CALL_CENTER_ONLY:** "[N] records in [plan]'s file list, as their only telephone number, [number] — a number the same file attaches to [K] other records[ and which NPPES associates with none of these providers, listing instead a different practice phone for each of the [J] spot-checked NPIs (NPPES snapshot [date])]. The file as published offers no direct way to reach these listings."
- **M6 ADDRESS_INFLATION:** "[Plan]'s file lists [Provider-as-published, NPI] at [341] simultaneous street addresses in [6] states[, while the federal NPPES registry (snapshot [date]) records [1] practice location]. We make no claim about where this provider actually practices; we report that the two records disagree, and that a directory entry with [341] addresses does not tell a patient where care is available."
- **M7 OUT_OF_AREA_LISTING:** "[N] of [M] records attached to [plan]'s ID in the issuer's file have no address inside the plan's CMS-filed service area or any adjacent county. Example: the file lists '[MinuteClinic Diagnostic of Indiana LLC]' under Wisconsin marketplace plan IDs with only [Indiana] addresses. These records were excluded from county rosters and reported here."
- **M8 ACCEPTING_UNKNOWN:** "[N] individual-provider records in [plan]'s file leave the CMS-required `accepting` field blank or 'unknown.' (Facility and group records are exempt from this field and are not counted.)"
- **M9 NPI_DEACTIVATED:** "The NPI [x] that [plan]'s file listed on [fetch date] appears in CMS's NPPES Deactivated NPI Report dated [report date] (deactivation date [d]) and is absent from the NPPES active file. NPIs are deactivated for many reasons — retirement, death, practice or entity changes — so this does not establish that the person or organization no longer provides care; it establishes that the issuer's current directory carries an identifier the federal registry has retired."
- **M10 TAXONOMY_MISMATCH:** "[Plan]'s file lists this NPI under '[Psychiatry]'; the NPPES registry (snapshot [date]) records only taxonomies in an unrelated group ('[Physical Therapist]'). One of the two databases is wrong about this provider's specialty; we do not know which."
- **Composite score:** "The Directory Integrity Score measures whether [Issuer]'s federally mandated machine-readable directory file is internally consistent, current, and usable as published. It is computed entirely from the issuer's own published file and dated federal registry snapshots. It is not a measure of care quality, of network adequacy, or of any individual provider."

---

## 7. Open items for v0 → v1

1. Run the NPPES full join → real prevalence for M9-deactivated and M10 (the two TBDs).
2. Build the specialty→taxonomy crosswalk (BH whitelist first); replace the 14.1% regex estimate with a taxonomy-grounded BH share.
3. Full-file streaming parses (ijson) to replace head-of-file sampling; re-verify M4/M5/M6 prevalence unbiased.
4. Decide adjacency buffer for M7 (county-adjacent vs state-adjacent) with sensitivity table.
5. Dental/vision plans: score separately (DentalOnlyPlan flag in Plan Attributes PUF) — their defect profiles (Guardian, DentaQuest, WY vision) differ enough to distort medical-plan comparisons.
6. Calibration pass after first full crawl: re-fit grade bands to the observed score distribution and publish the before/after.
