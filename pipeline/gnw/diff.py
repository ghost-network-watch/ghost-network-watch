"""Snapshot-over-snapshot diff: resolutions, new findings, grade movement.

Flags carry (source_sha256, record_idx), but insurers regenerate files every
month, so those keys never survive a snapshot. The stable identity of a
finding across months is:

    (issuer_id, metric, subcode, npi)

meaning "this type of problem, about this provider, in this insurer's file."
A key present last month and absent this month is RESOLVED (dated). Present
in both: PERSISTING. Present only now: NEW. Flags without an NPI (malformed
identifiers) and the attachment-grain out-of-area metric are diffed as
per-issuer counts instead of per-key.

Outputs under data/diff/<snapshot>/:
    flag_status.parquet     every key with status new|persisting|resolved
    resolved_flags.csv.gz   the resolved keys, dated (also a site download)
    count_changes.parquet   per (issuer, metric) count deltas incl. M7
    grade_changes.parquet   plan-county cells whose letter grade moved
    summary.json            headline numbers for the changes page and feed
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import duckdb

log = logging.getLogger("gnw.diff")

KEYED_METRICS = [
    "M3_PLACEHOLDER_VALUE", "M4_STALE_ATTESTATION", "M5_CALL_CENTER_ONLY",
    "M6_ADDRESS_INFLATION", "M8_ACCEPTING_UNKNOWN", "M9_NPI_REGISTRY_STATUS",
    "M10_TAXONOMY_MISMATCH",
]


def previous_snapshot(data_root: Path, snapshot: str) -> str | None:
    snaps = sorted(
        p.name for p in (data_root / "flags").iterdir()
        if p.is_dir() and p.name < snapshot and (p / "M3_PLACEHOLDER_VALUE.parquet").exists()
    )
    return snaps[-1] if snaps else None


def _register(con, data_root: Path, snapshot: str, prefix: str) -> None:
    flags = data_root / "flags" / snapshot
    union = " UNION ALL ".join(
        f"SELECT metric, subcode, npi, source_sha256 "
        f"FROM read_parquet('{flags / (m + '.parquet')}')"
        for m in KEYED_METRICS
    )
    con.execute(f"""
        CREATE TEMP TABLE {prefix}_file_issuers AS
        SELECT DISTINCT sha256, unnest(issuer_ids) AS issuer_id
        FROM read_json_auto('{data_root / "snapshots" / snapshot / "manifest.jsonl"}')
        WHERE sha256 IS NOT NULL AND role = 'provider'
    """)
    con.execute(f"""
        CREATE TEMP TABLE {prefix}_keys AS
        SELECT DISTINCT fi.issuer_id, f.metric, f.subcode, f.npi
        FROM ({union}) f
        JOIN {prefix}_file_issuers fi ON fi.sha256 = f.source_sha256
        WHERE f.npi IS NOT NULL
    """)
    m7 = flags / "M7_OUT_OF_AREA_LISTING.parquet"
    con.execute(f"""
        CREATE TEMP TABLE {prefix}_counts AS
        SELECT fi.issuer_id, f.metric, count(*) AS n
        FROM ({union}) f
        JOIN {prefix}_file_issuers fi ON fi.sha256 = f.source_sha256
        WHERE f.npi IS NULL
        GROUP BY 1, 2
        UNION ALL
        SELECT substr(plan_id, 1, 5), 'M7_OUT_OF_AREA_LISTING', count(*)
        FROM read_parquet('{m7}') GROUP BY 1
    """)
    con.execute(f"""
        CREATE VIEW {prefix}_scores AS
        SELECT scid, county_fips, scope, grade, score, thin_roster
        FROM read_parquet('{data_root / "scores" / snapshot / "plan_county_scores.parquet"}')
    """)


def build_diff(data_root: Path, snapshot: str, previous: str | None = None) -> dict:
    previous = previous or previous_snapshot(data_root, snapshot)
    out = data_root / "diff" / snapshot
    out.mkdir(parents=True, exist_ok=True)
    if previous is None:
        summary = {"snapshot": snapshot, "previous": None, "first_snapshot": True}
        (out / "summary.json").write_text(json.dumps(summary, indent=1))
        log.info("no previous snapshot; wrote first-snapshot marker")
        return summary

    con = duckdb.connect()
    con.execute("SET memory_limit='8GB'")
    con.execute("SET preserve_insertion_order=false")
    _register(con, data_root, snapshot, "new")
    _register(con, data_root, previous, "old")

    con.execute(f"""
        CREATE TEMP TABLE flag_status AS
        SELECT coalesce(n.issuer_id, o.issuer_id) AS issuer_id,
               coalesce(n.metric, o.metric) AS metric,
               coalesce(n.subcode, o.subcode) AS subcode,
               coalesce(n.npi, o.npi) AS npi,
               CASE WHEN o.npi IS NULL THEN 'new'
                    WHEN n.npi IS NULL THEN 'resolved'
                    ELSE 'persisting' END AS status,
               CASE WHEN n.npi IS NULL THEN '{snapshot}' END AS resolved_in,
               '{previous}' AS compared_to
        FROM new_keys n
        FULL OUTER JOIN old_keys o
          ON o.issuer_id = n.issuer_id AND o.metric = n.metric
         AND o.subcode = n.subcode AND o.npi = n.npi
    """)
    con.execute(
        f"COPY flag_status TO '{out / 'flag_status.parquet'}' "
        "(FORMAT PARQUET, COMPRESSION ZSTD)"
    )
    con.execute(
        f"""COPY (SELECT issuer_id, metric, subcode, npi, resolved_in, compared_to
        FROM flag_status WHERE status = 'resolved'
        ORDER BY issuer_id, metric, npi)
        TO '{out / 'resolved_flags.csv.gz'}' (FORMAT CSV, HEADER, COMPRESSION GZIP)"""
    )

    con.execute(f"""
        CREATE TEMP TABLE count_changes AS
        SELECT coalesce(n.issuer_id, o.issuer_id) AS issuer_id,
               coalesce(n.metric, o.metric) AS metric,
               coalesce(o.n, 0) AS n_previous, coalesce(n.n, 0) AS n_current,
               coalesce(n.n, 0) - coalesce(o.n, 0) AS delta
        FROM new_counts n
        FULL OUTER JOIN old_counts o
          ON o.issuer_id = n.issuer_id AND o.metric = n.metric
    """)
    con.execute(
        f"COPY count_changes TO '{out / 'count_changes.parquet'}' "
        "(FORMAT PARQUET, COMPRESSION ZSTD)"
    )

    con.execute(f"""
        CREATE TEMP TABLE grade_changes AS
        SELECT coalesce(n.scid, o.scid) AS scid,
               coalesce(n.county_fips, o.county_fips) AS county_fips,
               coalesce(n.scope, o.scope) AS scope,
               o.grade AS grade_previous, n.grade AS grade_current,
               o.score AS score_previous, n.score AS score_current
        FROM new_scores n
        FULL OUTER JOIN old_scores o
          ON o.scid = n.scid AND o.county_fips = n.county_fips AND o.scope = n.scope
        WHERE coalesce(o.grade, '') <> coalesce(n.grade, '')
    """)
    con.execute(
        f"COPY grade_changes TO '{out / 'grade_changes.parquet'}' "
        "(FORMAT PARQUET, COMPRESSION ZSTD)"
    )

    counts = dict(
        con.execute("SELECT status, count(*) FROM flag_status GROUP BY 1").fetchall()
    )
    improved, declined = con.execute("""
        SELECT
          count(*) FILTER (grade_current < grade_previous),
          count(*) FILTER (grade_current > grade_previous)
        FROM grade_changes
        WHERE scope = 'bh' AND grade_previous IS NOT NULL AND grade_current IS NOT NULL
    """).fetchone()
    top_resolvers = con.execute("""
        SELECT issuer_id, count(*) FROM flag_status WHERE status='resolved'
        GROUP BY 1 ORDER BY 2 DESC LIMIT 10
    """).fetchall()
    summary = {
        "snapshot": snapshot,
        "previous": previous,
        "first_snapshot": False,
        "resolved": counts.get("resolved", 0),
        "new": counts.get("new", 0),
        "persisting": counts.get("persisting", 0),
        "grades_improved": improved,
        "grades_declined": declined,
        "top_resolvers": [{"issuer_id": i, "resolved": n} for i, n in top_resolvers],
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=1))
    log.info(
        "diff %s vs %s: %d resolved, %d new, %d persisting, %d/%d grades up/down",
        snapshot, previous, summary["resolved"], summary["new"],
        summary["persisting"], improved, declined,
    )
    return summary
