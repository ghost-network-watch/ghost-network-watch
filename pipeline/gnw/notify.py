"""Issuer pre-notification bundles.

Rubric publication gate: at least 14 days before first publication, each
issuer's technical contact on file with CMS receives its complete
machine-readable flag export. This module GENERATES the bundles and email
drafts under data/notify/<snapshot>/<issuer_id>/. Nothing is sent from here;
sending is a deliberate, human-triggered step.

Bundle contents per issuer:
  summary.json        counts per metric + feed status + score rollup
  evidence_*.csv.gz   full record-level evidence rows for the issuer's files
                      (M7 as per-plan counts plus a capped sample)
  draft_email.txt     plain-text email draft for the CMS-listed Tech POC
"""

from __future__ import annotations

import csv
import json
import logging
from pathlib import Path

import duckdb

from .seed import PUF_CSV

log = logging.getLogger("gnw.notify")

RECORD_METRICS = [
    "M3_PLACEHOLDER_VALUE", "M4_STALE_ATTESTATION", "M5_CALL_CENTER_ONLY",
    "M6_ADDRESS_INFLATION", "M8_ACCEPTING_UNKNOWN", "M9_NPI_REGISTRY_STATUS",
    "M10_TAXONOMY_MISMATCH",
]

EMAIL_TEMPLATE = """\
To: {poc_email}
From: contact@ghostnetworkwatch.org
Subject: Pre-publication notice: directory integrity findings for HIOS issuer {issuer_id} ({snapshot} snapshot)

Hello,

You are listed in CMS's Machine-Readable URL PUF as the technical contact for
HIOS issuer {issuer_id}. Ghost Network Watch is an independent public-interest
project that audits the machine-readable provider directory files marketplace
issuers publish under 45 CFR 156.230(b).

We fetched your issuer's published directory file(s) during the {snapshot}
crawl and computed the findings attached to this message. We plan to publish
these findings, with per-plan and per-county integrity scores, on or after
{publish_date}. This notice gives you at least 14 days to review them first.

What the attachments contain:
- summary.json: counts per finding type and the score rollup
- evidence_*.csv.gz: every evidence row, each carrying the source file's
  SHA-256, the record index inside that file, and the observed values

Every finding describes values your published file carries, or a dated
disagreement between that file and a federal registry snapshot. Findings are
not claims about any individual provider.

If any finding is wrong, or if you fix the underlying data, reply to this
address. Responses are published verbatim alongside the findings they
dispute. Findings that no longer reproduce in a later monthly crawl are
marked resolved with the date. History is retained.

Methodology: https://ghostnetworkwatch.org/methodology/
Contact: contact@ghostnetworkwatch.org

Ghost Network Watch
"""


def build_notifications(
    data_root: Path, snapshot: str, publish_date: str, only_issuer: str | None = None
) -> Path:
    out_root = data_root / "notify" / snapshot
    out_root.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET memory_limit='8GB'")
    con.execute("SET preserve_insertion_order=false")
    flags = data_root / "flags" / snapshot
    for m in RECORD_METRICS:
        con.execute(
            f"CREATE VIEW {m.lower()} AS SELECT * FROM read_parquet('{flags / (m + '.parquet')}')"
        )
    con.execute(
        "CREATE VIEW m7 AS SELECT * FROM read_parquet("
        f"'{flags / 'M7_OUT_OF_AREA_LISTING.parquet'}')"
    )
    con.execute(
        "CREATE VIEW feed_flags AS SELECT * FROM read_parquet("
        f"'{flags / 'feed_flags.parquet'}')"
    )
    con.execute(
        "CREATE VIEW manifest AS SELECT * FROM read_json_auto("
        f"'{data_root / 'snapshots' / snapshot / 'manifest.jsonl'}')"
    )
    con.execute(
        "CREATE VIEW scores AS SELECT * FROM read_parquet("
        f"'{data_root / 'scores' / snapshot / 'plan_county_scores.parquet'}')"
    )
    con.execute("""
        CREATE TEMP TABLE file_issuers AS
        SELECT DISTINCT sha256, unnest(issuer_ids) AS issuer_id, len(issuer_ids) > 1 AS shared
        FROM manifest WHERE sha256 IS NOT NULL AND role = 'provider'
    """)

    pocs: dict[str, dict] = {}
    with open(PUF_CSV) as fh:
        for row in csv.DictReader(fh):
            pocs.setdefault(
                row["Issuer ID"],
                {"email": row["Tech POC Email"], "states": set(), "url": row["URL Submitted"]},
            )["states"].add(row["State"])

    issuer_ids = [only_issuer] if only_issuer else sorted(pocs)
    built = 0
    for iid in issuer_ids:
        if iid not in pocs:
            log.warning("issuer %s not in PUF", iid)
            continue
        has_files = con.execute(
            "SELECT count(*) FROM file_issuers WHERE issuer_id = ?", [iid]
        ).fetchone()[0]
        if not has_files:
            continue  # dental/out-of-scope or dead feed with nothing fetched
        dest = out_root / iid
        dest.mkdir(parents=True, exist_ok=True)

        summary: dict = {"issuer_id": iid, "snapshot": snapshot, "metrics": {}}
        for m in RECORD_METRICS:
            n = con.execute(f"""
                SELECT count(*) FROM {m.lower()} f
                JOIN file_issuers fi ON fi.sha256 = f.source_sha256
                WHERE fi.issuer_id = ?
            """, [iid]).fetchone()[0]
            summary["metrics"][m] = n
            if n:
                con.execute(f"""
                    COPY (
                      SELECT f.*, fi.shared AS shared_platform_file
                      FROM {m.lower()} f
                      JOIN file_issuers fi ON fi.sha256 = f.source_sha256
                      WHERE fi.issuer_id = '{iid}'
                    ) TO '{dest / ('evidence_' + m + '.csv.gz')}'
                    (FORMAT CSV, HEADER, COMPRESSION GZIP)
                """)
        m7n = con.execute("""
            SELECT count(*) FROM m7 WHERE substr(plan_id, 1, 5) = ?
        """, [iid]).fetchone()[0]
        summary["metrics"]["M7_OUT_OF_AREA_LISTING"] = m7n
        if m7n:
            con.execute(f"""
                COPY (
                  SELECT plan_id, count(*) AS out_of_area_attachments
                  FROM m7 WHERE substr(plan_id, 1, 5) = '{iid}' GROUP BY 1
                ) TO '{dest / 'evidence_M7_by_plan.csv.gz'}'
                (FORMAT CSV, HEADER, COMPRESSION GZIP)
            """)
            con.execute(f"""
                COPY (
                  SELECT * FROM m7 WHERE substr(plan_id, 1, 5) = '{iid}'
                  USING SAMPLE 50000 ROWS
                ) TO '{dest / 'evidence_M7_sample.csv.gz'}'
                (FORMAT CSV, HEADER, COMPRESSION GZIP)
            """)
        summary["feed"] = [
            {"metric": r[0], "subcode": r[1], "url": r[2]}
            for r in con.execute(
                "SELECT metric, subcode, url FROM feed_flags WHERE list_contains(issuer_ids, ?)",
                [iid],
            ).fetchall()
        ]
        summary["score_rollup"] = {
            scope: {"cells": c, "avg_score": avg}
            for scope, c, avg in con.execute("""
                SELECT scope, count(*), round(avg(score), 1)
                FROM scores WHERE substr(scid, 1, 5) = ? GROUP BY 1
            """, [iid]).fetchall()
        }
        (dest / "summary.json").write_text(json.dumps(summary, indent=1))
        (dest / "draft_email.txt").write_text(
            EMAIL_TEMPLATE.format(
                poc_email=pocs[iid]["email"], issuer_id=iid,
                snapshot=snapshot, publish_date=publish_date,
            )
        )
        built += 1
    log.info("notification bundles: %d issuers -> %s", built, out_root)
    return out_root
