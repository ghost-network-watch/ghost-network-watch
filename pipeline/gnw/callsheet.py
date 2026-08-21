"""Phone-calibration call sheets.

Samples provider records from the best- and worst-graded behavioral-health
cells so a manual phone study can compare lived reachability against the
file-based scores. Output: a CSV with one row per call to make, plus blank
outcome columns, and a PROTOCOL.txt describing a consistent procedure.

This file contains provider names and phone numbers quoted from insurers'
published files. It is a private research instrument; do not publish it.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

log = logging.getLogger("gnw.callsheet")

PROTOCOL = """\
Ghost Network Watch phone-calibration protocol (v0)

Goal: measure whether directory grades predict real reachability.
Design: {cells} F-graded cells and {cells} A-graded cells (BH scope, n>=30),
{per_cell} sampled listings each. Call every row the same way.

Procedure per call:
1. Call during business hours. Let it ring at least 60 seconds.
2. If an office answers, say: "I'm looking for a mental health appointment
   with [provider name]. Do they practice here, are they in network with
   [plan marketing name], and are they taking new patients?"
3. Do not book an appointment. If offered one, ask how many days out it
   would be, note the answer, and say you will call back after checking
   your schedule.
4. Fill the outcome columns immediately after each call.
5. One attempt per row on the first pass. Log a second attempt on another
   day for rows marked NO_ANSWER before treating them as unreachable.
6. Rows with no phone number or a placeholder number need no call: record
   rang=N with note "no dialable number published". That outcome is itself
   the measurement.

Outcome column values:
  rang: Y / N (N = disconnected, wrong number, fax, or dead line)
  reached_office: Y / N
  provider_known_here: Y / N / UNK
  in_network_confirmed: Y / N / UNK
  accepting_new: Y / N / UNK
  appointment_days: number, or blank
  notes: free text

Ethic: keep calls short, honest, and do not hold appointment slots.
"""


def build_callsheet(
    data_root: Path, snapshot: str, cells_per_grade: int = 5, per_cell: int = 10
) -> Path:
    out_dir = data_root / "callsheet" / snapshot
    out_dir.mkdir(parents=True, exist_ok=True)
    con = duckdb.connect()
    con.execute("SET memory_limit='8GB'")
    con.execute("SET preserve_insertion_order=false")
    pq = data_root / "parquet" / snapshot
    for name, path in {
        "scores": data_root / "scores" / snapshot / "plan_county_scores.parquet",
        "providers": pq / "providers" / "*.parquet",
        "addresses": pq / "provider_addresses" / "*.parquet",
        "file_dim": pq / "compact" / "file_dim.parquet",
        "scid_dim": pq / "compact" / "scid_dim.parquet",
        "attach_c": pq / "compact" / "attach.parquet",
        "rec_county_c": pq / "compact" / "rec_county.parquet",
    }.items():
        con.execute(f"CREATE VIEW {name} AS SELECT * FROM read_parquet('{path}')")

    con.execute(f"""
        CREATE TEMP TABLE target_cells AS
        WITH ranked AS (
          SELECT scid, county_fips, county_name, state, grade, n,
                 plan_marketing_name, issuer_name,
                 row_number() OVER (PARTITION BY grade ORDER BY n DESC, scid) AS rk
          FROM scores
          WHERE scope = 'bh' AND grade IN ('A', 'F') AND n >= 30
        )
        SELECT * FROM ranked WHERE rk <= {cells_per_grade}
    """)
    n_cells = con.execute("SELECT count(*) FROM target_cells").fetchone()[0]

    dest = out_dir / "callsheet.csv"
    con.execute(f"""
        COPY (
          WITH roster AS (
            SELECT tc.grade AS cell_grade, tc.scid, tc.plan_marketing_name,
                   tc.county_name, tc.state, tc.issuer_name,
                   fd.source_sha256, t.record_idx, p.npi, p.specialties,
                   p.accepting,
                   coalesce(p.name_first || ' ' || p.name_last, p.facility_name) AS provider_name,
                   row_number() OVER (
                     PARTITION BY tc.scid, tc.county_fips
                     ORDER BY t.fid, t.record_idx
                   ) AS rk
            FROM target_cells tc
            JOIN scid_dim sd ON sd.scid = tc.scid
            JOIN attach_c t ON t.scid_id = sd.scid_id
            JOIN rec_county_c rc
              ON rc.fid = t.fid AND rc.record_idx = t.record_idx
             AND rc.county = tc.county_fips::INTEGER
            JOIN file_dim fd ON fd.fid = t.fid
            JOIN providers p
              ON p.source_sha256 = fd.source_sha256 AND p.record_idx = t.record_idx
            WHERE p.specialties ILIKE '%psych%' OR p.specialties ILIKE '%counsel%'
               OR p.specialties ILIKE '%social work%' OR p.specialties ILIKE '%therap%'
          ),
          sampled AS (SELECT * FROM roster WHERE rk <= {per_cell})
          SELECT row_number() OVER () AS study_id,
                 s.cell_grade, s.plan_marketing_name AS plan, s.scid AS plan_id,
                 s.county_name, s.state, s.issuer_name,
                 s.provider_name, s.specialties, s.accepting AS file_says_accepting,
                 a.phone, a.address, a.city, a.state AS addr_state, a.zip, s.npi,
                 '' AS call_date, '' AS rang, '' AS reached_office,
                 '' AS provider_known_here, '' AS in_network_confirmed,
                 '' AS accepting_new, '' AS appointment_days, '' AS notes
          FROM sampled s
          LEFT JOIN addresses a
            ON a.source_sha256 = s.source_sha256 AND a.record_idx = s.record_idx
           AND a.addr_idx = 0
          ORDER BY s.cell_grade, s.scid, s.rk
        ) TO '{dest}' (FORMAT CSV, HEADER)
    """)
    n = con.execute(f"SELECT count(*) FROM '{dest}'").fetchone()[0]
    (out_dir / "PROTOCOL.txt").write_text(
        PROTOCOL.format(cells=cells_per_grade, per_cell=per_cell)
    )
    log.info("callsheet: %d cells, %d call rows -> %s", n_cells, n, dest)
    return dest
