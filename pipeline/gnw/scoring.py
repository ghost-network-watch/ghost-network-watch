"""Aggregation: evidence rows -> plan x county Directory Integrity Scores.

Implements rubric §3:
  cell           = (StandardComponentId, county FIPS from the QHP landscape —
                    the on-exchange denominator verified during scoping)
  penalty(r)     = min(1.0, w_max + 0.25 * Σ w_other)   [flags stack sublinearly]
  Component A    = 100 x (1 - mean penalty over county roster), per scope
  Component B    = 100 x (1 - 0.8 x out-of-area rate of the SCID)
                   [M7 records never enter rosters — they self-exclude, so
                    they are scored plan-wide or not at all]
  Score          = 0.8A + 0.2B
  Tier-B cap     = final = max(score, tier_a_only_score - 20)
  Score_uniform  = same with all weights 0.6 (weight-sensitivity check)
  n >= 30        -> numeric + grade; 10-29 -> grade + low-sample badge
                    (numeric suppressed when the flagged-share Wilson interval
                    spans > 25 points); n < 10 -> thin-roster fact, no score
  Grades         A 90+ / B 80+ / C 70+ / D 55+ / F <55  (X handled at feed level)

Scopes: 'all' and 'bh' (behavioral health = BH NUCC taxonomy on the NPI, or
BH string verdict when the string include-list matches), always side by side.

All heavy joins run on the integer-encoded compact tables (compact.py).
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb
import pyarrow as pa

from .bh import classify_specialty, is_bh_taxonomy
from .compact import connect as _duck

log = logging.getLogger("gnw.scoring")

SCORES_VERSION = "v0.1"
W7 = 0.8  # M7 weight, used in Component B


def _connect(data_root: Path, snapshot: str) -> duckdb.DuckDBPyConnection:
    con = _duck(data_root)
    pq = data_root / "parquet" / snapshot
    ref = data_root / "reference" / "parquet"
    flags = data_root / "flags" / snapshot
    views = {
        "providers": pq / "providers" / "*.parquet",
        "nppes": ref / "nppes.parquet",
        "nucc": ref / "nucc_taxonomy.parquet",
        "plan_county": ref / "plan_county.parquet",
        "file_dim": pq / "compact" / "file_dim.parquet",
        "scid_dim": pq / "compact" / "scid_dim.parquet",
        "attach_c": pq / "compact" / "attach.parquet",
        "rec_county_c": pq / "compact" / "rec_county.parquet",
        "m7": flags / "M7_OUT_OF_AREA_LISTING.parquet",
    }
    for name, path in views.items():
        con.execute(f"CREATE VIEW {name} AS SELECT * FROM read_parquet('{path}')")
    record_metrics = [
        "M3_PLACEHOLDER_VALUE", "M4_STALE_ATTESTATION", "M5_CALL_CENTER_ONLY",
        "M6_ADDRESS_INFLATION", "M8_ACCEPTING_UNKNOWN", "M9_NPI_REGISTRY_STATUS",
        "M10_TAXONOMY_MISMATCH",
    ]
    union = " UNION ALL ".join(
        f"SELECT source_sha256, record_idx, weight, evidence_strength "
        f"FROM read_parquet('{flags / (m + '.parquet')}')"
        for m in record_metrics
    )
    con.execute(f"CREATE VIEW record_flags AS {union}")
    return con


def _register_bh(con: duckdb.DuckDBPyConnection) -> None:
    distinct = [
        r[0]
        for r in con.execute(
            "SELECT DISTINCT specialties FROM providers WHERE specialties IS NOT NULL"
        ).fetchall()
    ]
    con.register(
        "spec_verdict",
        pa.Table.from_pylist(
            [{"specialties": s, "verdict": classify_specialty(s)} for s in distinct],
            schema=pa.schema([("specialties", pa.string()), ("verdict", pa.string())]),
        ),
    )
    bh_codes = [
        r[0] for r in con.execute("SELECT code FROM nucc").fetchall() if is_bh_taxonomy(r[0])
    ]
    con.register(
        "bh_codes",
        pa.Table.from_pylist(
            [{"code": c} for c in bh_codes], schema=pa.schema([("code", pa.string())])
        ),
    )
    con.execute("""
        CREATE TEMP TABLE bh_records AS
        WITH nppes_bh AS (
          SELECT DISTINCT t.npi
          FROM (SELECT npi, unnest(string_split(taxonomies, '|')) AS code
                FROM nppes WHERE taxonomies IS NOT NULL) t
          JOIN bh_codes b USING (code)
        )
        SELECT DISTINCT fd.fid, p.record_idx::INTEGER AS record_idx
        FROM providers p
        JOIN file_dim fd USING (source_sha256)
        LEFT JOIN spec_verdict v ON v.specialties = p.specialties
        LEFT JOIN nppes_bh nb ON nb.npi = p.npi
        WHERE nb.npi IS NOT NULL OR v.verdict = 'bh'
    """)


def build_scores(data_root: Path, snapshot: str) -> Path:
    out_dir = data_root / "scores" / snapshot
    out_dir.mkdir(parents=True, exist_ok=True)
    con = _connect(data_root, snapshot)
    _register_bh(con)
    log.info("bh_records: %d", con.execute("SELECT count(*) FROM bh_records").fetchone()[0])

    # Per-record penalties (weighted, tier-A-only, uniform), int-keyed.
    con.execute("""
        CREATE TEMP TABLE penalties AS
        SELECT fd.fid, rf.record_idx::INTEGER AS record_idx,
               least(1.0, max(weight) + 0.25 * (sum(weight) - max(weight))) AS pen,
               least(1.0, coalesce(max(weight) FILTER (evidence_strength <> 'E3'), 0)
                     + 0.25 * (coalesce(sum(weight) FILTER (evidence_strength <> 'E3'), 0)
                               - coalesce(max(weight) FILTER (evidence_strength <> 'E3'), 0))) AS pen_a,
               least(1.0, 0.6 + 0.15 * (count(*) - 1)) AS pen_u
        FROM record_flags rf
        JOIN file_dim fd USING (source_sha256)
        GROUP BY 1, 2
    """)

    # Cells from the landscape, int-encoded.
    con.execute("""
        CREATE TEMP TABLE cells AS
        SELECT d.scid_id,
               regexp_replace(pc.fips, '[^0-9]', '', 'g')::INTEGER AS county,
               any_value(pc.state) AS state
        FROM plan_county pc
        JOIN scid_dim d ON d.scid = substr(pc.plan_id, 1, 14)
        GROUP BY 1, 2
    """)

    # Roster: attached records placed in their plan's landscape counties.
    con.execute("""
        CREATE TEMP TABLE roster AS
        SELECT c.scid_id, c.county, t.fid, t.record_idx,
               (b.record_idx IS NOT NULL) AS is_bh
        FROM cells c
        JOIN attach_c t USING (scid_id)
        JOIN rec_county_c rc
          ON rc.fid = t.fid AND rc.record_idx = t.record_idx AND rc.county = c.county
        LEFT JOIN bh_records b
          ON b.fid = t.fid AND b.record_idx = t.record_idx
    """)
    log.info("roster rows: %d", con.execute("SELECT count(*) FROM roster").fetchone()[0])

    # Component B inputs: plan-wide out-of-area rate over mappable attached records.
    con.execute("""
        CREATE TEMP TABLE comp_b AS
        WITH ooa AS (
          SELECT DISTINCT d.scid_id, fd.fid, m.record_idx::INTEGER AS record_idx
          FROM m7 m
          JOIN file_dim fd USING (source_sha256)
          JOIN scid_dim d ON d.scid = m.plan_id
        ),
        mappable AS (
          SELECT DISTINCT t.scid_id, t.fid, t.record_idx
          FROM attach_c t
          JOIN (SELECT DISTINCT fid, record_idx FROM rec_county_c) m
            USING (fid, record_idx)
        )
        SELECT m.scid_id,
               count(*) AS n_attached,
               count(o.record_idx) AS n_out_of_area,
               count(o.record_idx)::DOUBLE / count(*) AS ooa_rate
        FROM mappable m
        LEFT JOIN ooa o USING (scid_id, fid, record_idx)
        GROUP BY 1
    """)

    # Cell scores per scope.
    con.execute(f"""
        CREATE TEMP TABLE cell_scores AS
        WITH per_scope AS (
          SELECT scid_id, county, state, scope, count(*) AS n,
                 avg(pen) AS mean_pen, avg(pen_a) AS mean_pen_a, avg(pen_u) AS mean_pen_u,
                 avg(flagged) AS flag_rate
          FROM (
            SELECT r.*, s.scope,
                   coalesce(p.pen, 0) AS pen, coalesce(p.pen_a, 0) AS pen_a,
                   coalesce(p.pen_u, 0) AS pen_u,
                   (p.pen IS NOT NULL)::INT AS flagged
            FROM roster r
            CROSS JOIN (SELECT 'all' AS scope UNION ALL SELECT 'bh') s
            LEFT JOIN penalties p USING (fid, record_idx)
            WHERE s.scope = 'all' OR r.is_bh
          )
          GROUP BY 1, 2, 3, 4
        ),
        scored AS (
          SELECT ps.*,
                 100 * (1 - ps.mean_pen) AS comp_a,
                 100 * (1 - {W7} * coalesce(cb.ooa_rate, 0)) AS comp_b,
                 cb.ooa_rate,
                 1.96 * sqrt(ps.flag_rate * (1 - ps.flag_rate) / ps.n + 0.9604 / (ps.n * ps.n))
                   / (1 + 3.8416 / ps.n) AS wilson_hw
          FROM per_scope ps
          LEFT JOIN comp_b cb USING (scid_id)
        ),
        final AS (
          SELECT *,
                 0.8 * comp_a + 0.2 * comp_b AS score_raw,
                 greatest(0.8 * comp_a + 0.2 * comp_b,
                          (0.8 * 100 * (1 - mean_pen_a) + 0.2 * comp_b) - 20) AS score_final,
                 0.8 * 100 * (1 - mean_pen_u) + 0.2 * comp_b AS score_uniform
          FROM scored
        )
        SELECT scid_id, county, state, scope, n,
               round(comp_a, 1) AS component_a, round(comp_b, 1) AS component_b,
               round(ooa_rate, 4) AS out_of_area_rate,
               round(score_raw, 1) AS score_weighted,
               round(score_final, 1) AS score,
               round(score_uniform, 1) AS score_uniform,
               CASE WHEN n < 10 THEN NULL
                    WHEN score_final >= 90 THEN 'A' WHEN score_final >= 80 THEN 'B'
                    WHEN score_final >= 70 THEN 'C' WHEN score_final >= 55 THEN 'D'
                    ELSE 'F' END AS grade,
               (n >= 10 AND n < 30) AS low_sample,
               (n >= 10 AND n < 30 AND 2 * wilson_hw * 100 > 25) AS numeric_suppressed,
               (n < 10) AS thin_roster,
               '{SCORES_VERSION}' AS scores_version
        FROM final
    """)
    dest = out_dir / "plan_county_scores.parquet"
    con.execute(
        f"""COPY (
          SELECT sd.scid, lpad(cs.county::VARCHAR, 5, '0') AS county_fips,
                 cs.* EXCLUDE (scid_id, county),
                 pc.issuer_name, pc.plan_marketing_name, pc.metal_level, pc.county_name
          FROM cell_scores cs
          JOIN scid_dim sd USING (scid_id)
          LEFT JOIN (
            SELECT substr(plan_id, 1, 14) AS scid,
                   regexp_replace(fips, '[^0-9]', '', 'g')::INTEGER AS county,
                   any_value(issuer_name) AS issuer_name,
                   any_value(plan_marketing_name) AS plan_marketing_name,
                   any_value(metal_level) AS metal_level,
                   any_value(county_name) AS county_name
            FROM plan_county GROUP BY 1, 2
          ) pc ON pc.scid = sd.scid AND pc.county = cs.county
        ) TO '{dest}' (FORMAT PARQUET, COMPRESSION ZSTD)"""
    )
    n = con.execute(f"SELECT count(*) FROM '{dest}'").fetchone()[0]
    log.info("plan_county_scores: %d cell-scope rows -> %s", n, dest)
    return dest
