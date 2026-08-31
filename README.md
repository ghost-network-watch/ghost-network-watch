# Ghost Network Watch

**[ghostnetworkwatch.org](https://ghostnetworkwatch.org)** — a continuous public
integrity audit of the provider directories that US health insurers are legally
required to publish, with mental health first.

Every insurer selling plans on HealthCare.gov must publish its full provider
directory as machine-readable JSON (45 CFR 156.230(c), which incorporates the
directory contents required by 156.230(b)) and update it at least monthly (CMS
Letter to Issuers). This project downloads every one of those files each
month, archives them with cryptographic fingerprints, checks every entry for
problems a patient would hit, and publishes a Directory Integrity Score for
every plan in every county, with downloadable evidence behind every claim.

From the August 2026 snapshot: 15.6 million provider records audited across
30 states; 31% of plan-county pairs list fewer than 10 mental health
providers; 8,159 listed providers carry federally retired identifiers; one
insurer's file has placeholder phone numbers on half its entries while
marking 90% of records as accepting new patients.

## What this repository contains

```
pipeline/       The whole engine, one Python package (gnw)
  gnw/crawl     polite fetcher + content-addressed evidence store
  gnw/parse     streaming JSON -> Parquet (files reach 1GB+)
  gnw/reference NPPES registry, NUCC taxonomy, CMS plan/service-area files
  gnw/flags     the ten integrity checks -> evidence rows
  gnw/scoring   evidence rows -> plan-county scores and grades
  gnw/diff      month-over-month resolutions and grade movement
  gnw/site      static site generator (the public website)
  gnw/notify    per-insurer pre-publication notification bundles
site/           Templates and assets for ghostnetworkwatch.org
scoping/        The August 2026 feasibility study: findings, evidence,
                the scoring rubric, verification artifacts
infra/          AWS CDK stacks for the hosted monthly run (optional)
ops/            run_monthly.sh: the whole month in one command
```

## Reproduce everything

Every input is public. One command per stage:

```bash
python -m venv .venv && .venv/bin/pip install -e pipeline/ \
    requests ijson pyarrow duckdb jinja2 openpyxl

SNAP=$(date -u +%Y-%m)
python -m gnw.cli crawl    --snapshot $SNAP   # fetch every mandated file (~90GB content)
python -m gnw.cli parse    --snapshot $SNAP   # -> Parquet tables
python -m gnw.cli refs                        # NPPES, NUCC, CMS PUFs, crosswalks
python -m gnw.cli compact  --snapshot $SNAP   # integer join tables
python -m gnw.cli flags    --snapshot $SNAP   # the ten checks -> evidence rows
python -m gnw.cli score    --snapshot $SNAP   # plan-county scores
python -m gnw.cli diff     --snapshot $SNAP   # resolutions vs previous month
python -m gnw.cli site     --snapshot $SNAP   # build the website
```

Or all of it: `ops/run_monthly.sh`. Expect a few hours, ~100GB of transfer,
and ~150GB of scratch disk. Details in ARCHITECTURE.md; every scoring rule
in the site's methodology page and `scoping/evidence/scoring_rubric_v0.md`.

## Method in one paragraph

Flags never assert why data is wrong, only what a dated file shows. Every
flag carries the SHA-256 of the archived source file, the record's position
in it, and the observed values, so every published claim is independently
checkable. Internal-consistency checks carry the scores; disagreements with
the federal registry are capped, because the registry lags too. Insurers
receive their complete flag export at least 14 days before publication, and
disputes are published verbatim.

## Corrections

Open a [correction request](../../issues/new?template=correction-request.yml)
or email contact@ghostnetworkwatch.org. Findings that stop reproducing in a
later monthly crawl are marked resolved with the date; history is retained.

## Citing and licensing

Cite as: Ghost Network Watch, Directory Integrity Scores, [month] snapshot,
ghostnetworkwatch.org.

- Code: Apache License 2.0 (see LICENSE)
- Scores and evidence exports: CC0 1.0 (public domain dedication)
- Site text and charts: CC BY 4.0, credit "Ghost Network Watch"
- Source text quoted inside evidence rows remains the insurers' published
  data, reproduced for accountability purposes with provenance

Built and operated by Soorena Sasani as an independent public-interest
project. Not affiliated with CMS, any insurer, or any vendor.
