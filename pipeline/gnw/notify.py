"""Issuer pre-notification bundles.

Rubric publication gate: at least 14 days before first publication, each
issuer's technical contact on file with CMS receives its complete
machine-readable flag export. This module GENERATES the bundles and email
drafts under data/notify/<snapshot>/<issuer_id>/. Nothing is sent from here;
sending is a deliberate, human-triggered step.

Bundle contents per issuer:
  summary.json        counts per metric + feed status + score rollup + which of
                      the issuer's files are co-published by other issuers
  evidence_*.csv.gz   record-level evidence rows attributed to this issuer by
                      the plan_id each record carries (M7 as per-plan counts
                      plus a capped sample)
  evidence_*_unattributed.csv.gz
                      rows in the issuer's files that no plan_id claims, so they
                      cannot be pinned on one issuer. Excluded from the counts
                      and only written when non-empty.
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
project that audits the machine-readable provider directory files that issuers
in the Federally-facilitated Exchange publish under 45 CFR 156.230(c).

We fetched your issuer's published directory file(s) during the {snapshot}
crawl and computed the findings attached to this message. We plan to publish
these findings, with per-plan and per-county integrity scores, on or after
{publish_date}. This notice gives you at least 14 days to review them first.

What the attachments contain:
- summary.json: counts per finding type, which of your files are also published
  by other issuers, and the score rollup
- evidence_*.csv.gz: every evidence row, each carrying the source file's
  SHA-256, the record index inside that file, and the observed values
- evidence_*_unattributed.csv.gz: only if present, see below

How records are attributed to you. Each record is attributed to the issuer
named in that record's own plan_id. If a file you publish is also published by
other issuers, which summary.json lists by HIOS ID, then rows in it belonging
to those issuers are not counted against you and are not included here. A small
number of records carry no plan_id we can resolve; those cannot be pinned on
any single issuer, so they are sent to every issuer publishing the file and are
kept in separate _unattributed files, excluded from your counts.

One note on units. Every count in summary.json is a number of provider records,
except that out-of-area listings are also reported as attachments, because one
record can be listed under many plans. The attachment number is therefore much
larger than the record number, and it is the rate, not either count, that
affects a score.

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
    compact = data_root / "parquet" / snapshot / "compact"
    for name, path in {
        "file_dim": compact / "file_dim.parquet",
        "scid_dim": compact / "scid_dim.parquet",
        "attach": compact / "attach.parquet",
    }.items():
        con.execute(f"CREATE VIEW {name} AS SELECT * FROM read_parquet('{path}')")

    con.execute("""
        CREATE TEMP TABLE file_issuers AS
        SELECT DISTINCT sha256, unnest(issuer_ids) AS issuer_id, len(issuer_ids) > 1 AS shared
        FROM manifest WHERE sha256 IS NOT NULL AND role = 'provider'
    """)

    # Which issuer a flagged record actually belongs to, taken from the plan_id
    # the record itself lists. This is the same attribution the scoring stage
    # uses, and it is the only correct one.
    #
    # Selecting a bundle's evidence by file instead was wrong, and badly so.
    # 110 of 185 issuers publish a file that other issuers also publish, and one
    # platform serves the same file for 24 issuers. File-scoping handed every one
    # of those 24 the whole file's rows: issuer 13484 received 869
    # M6_ADDRESS_INFLATION rows of which only 108 were its own, overstating its
    # problem roughly eightfold and inviting an easy, correct rebuttal. The rows
    # are public either way, so nothing was disclosed that was not already
    # published, but a notice is worthless if the recipient can show most of it
    # is about somebody else.
    con.execute("""
        CREATE TEMP TABLE record_owner AS
        SELECT DISTINCT a.fid, a.record_idx, substr(s.scid, 1, 5) AS issuer_id
        FROM attach a JOIN scid_dim s USING (scid_id)
    """)
    log.info("record_owner: %d (record, issuer) pairs",
             con.execute("SELECT count(*) FROM record_owner").fetchone()[0])

    # Denominator for the M7 rate: every record-to-plan attachment belonging to
    # an issuer's plans. Precomputed once because deriving it per issuer means
    # 185 scans of a 21-million-row table.
    con.execute("""
        CREATE TEMP TABLE issuer_attachments AS
        SELECT substr(s.scid, 1, 5) AS issuer_id, count(*) AS n
        FROM attach a JOIN scid_dim s USING (scid_id)
        GROUP BY 1
    """)

    # Per metric, the flagged rows resolved to their owning issuer, plus the rows
    # in a file that no plan_id claims. An unclaimed row cannot be pinned on one
    # issuer, so it goes to every issuer publishing that file, clearly labelled.
    for m in RECORD_METRICS:
        con.execute(f"""
            CREATE TEMP TABLE attr_{m.lower()} AS
            SELECT f.*, o.issuer_id
            FROM {m.lower()} f
            JOIN file_dim fd USING (source_sha256)
            JOIN record_owner o ON o.fid = fd.fid AND o.record_idx = f.record_idx
        """)
        con.execute(f"""
            CREATE TEMP TABLE orphan_{m.lower()} AS
            SELECT f.*, fi.issuer_id
            FROM {m.lower()} f
            JOIN file_dim fd USING (source_sha256)
            JOIN file_issuers fi ON fi.sha256 = f.source_sha256
            WHERE NOT EXISTS (
                SELECT 1 FROM record_owner o
                WHERE o.fid = fd.fid AND o.record_idx = f.record_idx
            )
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
            n = con.execute(
                f"SELECT count(*) FROM attr_{m.lower()} WHERE issuer_id = ?", [iid]
            ).fetchone()[0]
            n_orphan = con.execute(
                f"SELECT count(*) FROM orphan_{m.lower()} WHERE issuer_id = ?", [iid]
            ).fetchone()[0]
            summary["metrics"][m] = {"yours": n, "unattributed_in_your_files": n_orphan}
            if n:
                con.execute(f"""
                    COPY (
                      SELECT * EXCLUDE (issuer_id) FROM attr_{m.lower()}
                      WHERE issuer_id = '{iid}'
                    ) TO '{dest / ('evidence_' + m + '.csv.gz')}'
                    (FORMAT CSV, HEADER, COMPRESSION GZIP)
                """)
            if n_orphan:
                con.execute(f"""
                    COPY (
                      SELECT * EXCLUDE (issuer_id) FROM orphan_{m.lower()}
                      WHERE issuer_id = '{iid}'
                    ) TO '{dest / ('evidence_' + m + '_unattributed.csv.gz')}'
                    (FORMAT CSV, HEADER, COMPRESSION GZIP)
                """)
        m7n = con.execute("""
            SELECT count(*) FROM m7 WHERE substr(plan_id, 1, 5) = ?
        """, [iid]).fetchone()[0]
        # M7 was always attributed by plan_id, so it needs no orphan split.
        #
        # It does need different units. M7 has one row per record per plan, while
        # every other metric has one row per record, and reporting both as a bare
        # count put "144,968,156" next to "34,712" under one heading for the
        # largest issuer. That reads as a bug, and it invites the reply that we
        # are inflating findings. The count that compares to the other metrics is
        # distinct records; the attachment count is reported separately and
        # labelled, alongside the rate, which is what actually drives the score.
        m7_records = con.execute("""
            SELECT count(DISTINCT source_sha256 || ':' || record_idx)
            FROM m7 WHERE substr(plan_id, 1, 5) = ?
        """, [iid]).fetchone()[0]
        m7_denom = con.execute(
            "SELECT coalesce(max(n), 0) FROM issuer_attachments WHERE issuer_id = ?", [iid]
        ).fetchone()[0]
        summary["metrics"]["M7_OUT_OF_AREA_LISTING"] = {
            "yours": m7_records,
            "unattributed_in_your_files": 0,
            "unit": "provider records, comparable to the other metrics",
            "out_of_area_attachments": m7n,
            "plan_attachments_considered": m7_denom,
            "out_of_area_rate": round(m7n / m7_denom, 4) if m7_denom else None,
        }
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
        # Which of this issuer's files are also published by someone else, and by
        # whom. Named explicitly so a recipient can see why a file it publishes
        # contains records it does not recognise, and can check our arithmetic.
        summary["files"] = [
            {
                "sha256": sha,
                "co_published_by": sorted(set(co.split(",")) - {iid}),
            }
            for sha, co in con.execute("""
                SELECT f.sha256, string_agg(DISTINCT o.issuer_id, ',')
                FROM file_issuers f
                JOIN file_issuers o ON o.sha256 = f.sha256
                WHERE f.issuer_id = ?
                GROUP BY 1 ORDER BY 1
            """, [iid]).fetchall()
        ]
        summary["shared_file_count"] = sum(
            1 for f in summary["files"] if f["co_published_by"]
        )
        summary["attribution"] = (
            "Record-level findings are attributed to the issuer named in each "
            "record's own plan_id. Rows in a file you publish that belong to "
            "another issuer are not included here. Rows that no plan_id claims "
            "appear in the *_unattributed files."
        )
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
