"""Compact integer-encoded join tables.

provider_plans is ~610M rows and every row carries a 64-char sha and a 14+ char
plan id — joining on those spilled 93GB of DuckDB temp and died. This step
builds, once per snapshot:

  compact/file_dim.parquet   fid (INT)      <-> source_sha256
  compact/scid_dim.parquet   scid_id (INT)  <-> 14-char StandardComponentId
  compact/attach.parquet     DISTINCT (fid, record_idx, scid_id), PY2026 only
  compact/rec_county.parquet DISTINCT (fid, record_idx, county INT fips)

M7 and scoring join on ints; dims decode back to strings only at output time.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

log = logging.getLogger("gnw.compact")


def connect(data_root: Path) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET memory_limit='10GB'")
    con.execute("SET preserve_insertion_order=false")
    tmp = data_root / "tmp" / "duckdb"
    tmp.mkdir(parents=True, exist_ok=True)
    con.execute(f"SET temp_directory='{tmp}'")
    con.execute("SET max_temp_directory_size='60GB'")
    return con


def build_compact(data_root: Path, snapshot: str) -> Path:
    pq = data_root / "parquet" / snapshot
    out = pq / "compact"
    out.mkdir(parents=True, exist_ok=True)
    con = connect(data_root)
    con.execute(
        f"CREATE VIEW providers AS SELECT * FROM read_parquet('{pq / 'providers' / '*.parquet'}')"
    )
    con.execute(
        f"CREATE VIEW pplans AS SELECT * FROM read_parquet('{pq / 'provider_plans' / '*.parquet'}')"
    )
    con.execute(
        f"CREATE VIEW addresses AS SELECT * FROM read_parquet('{pq / 'provider_addresses' / '*.parquet'}')"
    )
    ref = data_root / "reference" / "parquet"
    con.execute(
        f"CREATE VIEW zip_county AS SELECT * FROM read_parquet('{ref / 'zip_county.parquet'}')"
    )

    con.execute("""
        CREATE TEMP TABLE file_dim AS
        SELECT source_sha256,
               row_number() OVER (ORDER BY source_sha256)::INTEGER AS fid
        FROM (SELECT DISTINCT source_sha256 FROM providers)
    """)
    con.execute("""
        CREATE TEMP TABLE scid_dim AS
        SELECT scid, row_number() OVER (ORDER BY scid)::INTEGER AS scid_id
        FROM (
          SELECT DISTINCT substr(plan_id, 1, 14) AS scid
          FROM pplans
          WHERE upper(coalesce(plan_id_type,'')) LIKE 'HIOS%'
            AND regexp_matches(substr(plan_id, 1, 14), '^\\d{5}[A-Z]{2}\\d{7}$')
        )
    """)
    con.execute(f"COPY file_dim TO '{out / 'file_dim.parquet'}' (FORMAT PARQUET)")
    con.execute(f"COPY scid_dim TO '{out / 'scid_dim.parquet'}' (FORMAT PARQUET)")

    con.execute(f"""
        COPY (
          SELECT DISTINCT f.fid, p.record_idx::INTEGER AS record_idx, s.scid_id
          FROM pplans p
          JOIN file_dim f USING (source_sha256)
          JOIN scid_dim s ON s.scid = substr(p.plan_id, 1, 14)
          WHERE upper(coalesce(p.plan_id_type,'')) LIKE 'HIOS%'
            AND (p.years IS NULL OR p.years LIKE '%2026%')
        ) TO '{out / 'attach.parquet'}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    n = con.execute(f"SELECT count(*) FROM '{out / 'attach.parquet'}'").fetchone()[0]
    log.info("attach: %d distinct (fid, record, scid) rows", n)

    con.execute(f"""
        COPY (
          SELECT DISTINCT f.fid, a.record_idx::INTEGER AS record_idx,
                 z.county_fips::INTEGER AS county
          FROM addresses a
          JOIN file_dim f USING (source_sha256)
          JOIN zip_county z
            ON z.zip = substr(regexp_replace(coalesce(a.zip,''), '[^0-9]', '', 'g'), 1, 5)
        ) TO '{out / 'rec_county.parquet'}' (FORMAT PARQUET, COMPRESSION ZSTD)
    """)
    m = con.execute(f"SELECT count(*) FROM '{out / 'rec_county.parquet'}'").fetchone()[0]
    log.info("rec_county: %d distinct (fid, record, county) rows", m)
    return out
