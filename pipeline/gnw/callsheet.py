"""Phone-calibration call sheets.

Samples provider records from well-graded and poorly-graded behavioral-health
cells so a manual phone study can compare lived reachability against the
file-based scores. Output: a CSV with one row per call to make, a
sample_summary.csv documenting how cells were selected and excluded, and a
PROTOCOL.txt describing a consistent procedure.

Sampling rules, and why each exists (all five were bugs in v0, found when the
2026-08 sheet turned out to be 7 phone numbers at one insurer):

  1. Both arms must be callable. v0 took A and F cells with no phone filter,
     and every F row came back with phone "null", so the poor arm had zero
     possible calls and the study could only measure A cells. Cells now
     qualify only if they hold at least `per_cell` distinct dialable numbers,
     and the poor arm draws from D or F.
  2. One cell per issuer per arm. v0 ranked cells by roster size, and the
     biggest cells are near-identical plan variants of one insurer in one
     metro, so five "independent" cells shared one network.
  3. One cell per state per arm, for geographic spread on top of that.
  4. Random-but-reproducible row choice. v0 took the first N records in file
     order; file order clusters by practice, which is what collapsed 50 rows
     into 7 offices. Rows are now ordered by a hash, so the sample is stable
     across reruns without being biased toward the top of the file.
  5. One row per phone number and one row per NPI, sheet-wide, so no number
     is dialed twice and no provider is asked about twice.

The undialable records dropped by rule 1 are not lost: sample_summary.csv
keeps every candidate cell's dialable share, which is itself a finding.

This file contains provider names and phone numbers quoted from insurers'
published files. It is a private research instrument; do not publish it.
"""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb

from .scoring import _register_bh

log = logging.getLogger("gnw.callsheet")

# Digits-only form of a published phone number.
_D = "regexp_replace(coalesce(a.phone, ''), '[^0-9]', '', 'g')"

# Dialable: a real 10-digit NANP number, not a repeated-digit placeholder
# (9999999999, 0000000000) and not a 555 fictional exchange.
_DIALABLE = (
    "len(d) = 10 AND d <> repeat(substr(d, 1, 1), 10) "
    "AND substr(d, 1, 3) NOT IN ('999', '000', '555')"
)

# Toll-free numbers reach a carrier or answering service rather than the
# listed office. Kept in the sample (a patient dialing the published number
# hits the same thing) but tagged so analysis can split on it.
_TOLL_FREE = "substr(d, 1, 3) IN ('800', '888', '877', '866', '855', '844', '833')"

PROTOCOL = """\
Ghost Network Watch phone-calibration protocol (v1)

Goal: measure whether directory grades predict real reachability.

Design: {cells} well-graded cells (A) and {cells} poorly-graded cells (D or F),
behavioral-health scope, n>=30 listed providers. One cell per insurer and one
per state within each arm, so the two arms are not the same network wearing
different plan names. {per_cell} listings sampled per cell, every one carrying
a dialable published number. Rows are one per phone number and one per
provider across the whole sheet, so no office is called twice.

Cells whose listings carry no dialable number at all are excluded from
calling and recorded in sample_summary.csv instead. That exclusion is a
finding, not a gap: a directory nobody can dial fails before the phone study
starts.

Procedure per call:
1. Call during business hours in the row's local time. Let it ring at least
   60 seconds.
2. Ask the shopper's question, not the patient's: "I'm shopping for a
   marketplace plan and I'd like to see [provider name]. Which insurers are
   they in network with, and are they taking new patients?" Letting the office
   name its own carriers avoids reading a list of plan variants at a
   receptionist, and it is what a real person shopping for coverage asks.
3. Record the answer at the carrier level (for example "Oscar", "Aetna").
   Plan variants inside one carrier almost always share a network, so a
   carrier-level answer resolves every row for that provider.
4. Do not book an appointment. If offered one, ask how many days out it would
   be, note the answer, and say you will call back after checking your
   schedule.
5. Fill the outcome columns immediately after each call.
6. One attempt per row on the first pass. Log a second attempt on another day
   for rows marked no answer before treating them as unreachable.

Outcome ladder, recording the furthest point reached:
  dead line / wrong number   never rang, or reached a fax or a non-office
  no answer / voicemail      rang, no human
  provider unknown here      a human answered and did not recognize them
  not in network             provider is real but not in that carrier
  not taking new patients    in network, panel closed
  taking new patients        also record days to first opening
  unclear                    anything else, explain in notes

Blinding: the caller should not see the cell grade while dialing. The Calls
tab of callsheet_easy.xlsx omits it deliberately; grades stay on the
Reference tab for analysis afterwards.

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
    ref = data_root / "reference" / "parquet"
    for name, path in {
        "scores": data_root / "scores" / snapshot / "plan_county_scores.parquet",
        "providers": pq / "providers" / "*.parquet",
        "addresses": pq / "provider_addresses" / "*.parquet",
        "file_dim": pq / "compact" / "file_dim.parquet",
        "scid_dim": pq / "compact" / "scid_dim.parquet",
        "attach_c": pq / "compact" / "attach.parquet",
        "rec_county_c": pq / "compact" / "rec_county.parquet",
        "nppes": ref / "nppes.parquet",
        "nucc": ref / "nucc_taxonomy.parquet",
    }.items():
        con.execute(f"CREATE VIEW {name} AS SELECT * FROM read_parquet('{path}')")

    # Use the scorer's own behavioral-health definition (BH NUCC taxonomy on
    # the NPI, or a specialty string classified as BH) rather than a local
    # keyword match. A cruder filter here would sample a different population
    # than the grade describes, which would break the calibration this study
    # exists to measure.
    _register_bh(con)

    # Candidate cells: one per issuer per arm (the largest such cell), then the
    # 40 largest per arm, to bound the roster join below.
    con.execute("""
        CREATE TEMP TABLE cand AS
        WITH base AS (
          SELECT scid, county_fips, county_name, state, grade, n,
                 plan_marketing_name, issuer_name, substr(scid, 1, 5) AS issuer_id,
                 CASE WHEN grade = 'A' THEN 'good' ELSE 'poor' END AS arm
          FROM scores
          WHERE scope = 'bh' AND n >= 30 AND grade IN ('A', 'D', 'F')
        ),
        per_issuer AS (
          SELECT *, row_number() OVER (
                     PARTITION BY arm, issuer_id
                     ORDER BY hash(scid || county_fips)) AS ir
          FROM base
        ),
        capped AS (
          SELECT *, row_number() OVER (
                     PARTITION BY arm ORDER BY hash(issuer_id || county_fips)) AS ar
          FROM per_issuer WHERE ir = 1
        )
        SELECT * FROM capped WHERE ar <= 40
    """)

    con.execute(f"""
        CREATE TEMP TABLE roster AS
        SELECT c.arm, c.grade AS cell_grade, c.scid, c.county_fips, c.county_name,
               c.state, c.plan_marketing_name, c.issuer_name, c.issuer_id,
               p.npi, p.specialties, p.accepting,
               coalesce(p.name_first || ' ' || p.name_last, p.facility_name)
                 AS provider_name,
               a.phone, a.address, a.city, a.state AS addr_state, a.zip,
               {_D} AS d
        FROM cand c
        JOIN scid_dim sd ON sd.scid = c.scid
        JOIN attach_c t ON t.scid_id = sd.scid_id
        JOIN rec_county_c rc
          ON rc.fid = t.fid AND rc.record_idx = t.record_idx
         AND rc.county = c.county_fips::INTEGER
        JOIN file_dim fd ON fd.fid = t.fid
        JOIN providers p
          ON p.source_sha256 = fd.source_sha256 AND p.record_idx = t.record_idx
        LEFT JOIN addresses a
          ON a.source_sha256 = fd.source_sha256 AND a.record_idx = t.record_idx
         AND a.addr_idx = 0
        JOIN bh_records b ON b.fid = t.fid AND b.record_idx = t.record_idx
    """)
    con.execute(f"""
        CREATE TEMP TABLE roster2 AS
        SELECT *, ({_DIALABLE}) AS dialable, ({_TOLL_FREE}) AS toll_free,
               (provider_name IS NULL OR trim(provider_name) = '') AS nameless
        FROM roster
    """)

    # Per-cell dialable inventory. distinct_numbers, not row count, is the
    # gate: a cell with 300 listings behind 7 numbers buys 7 calls.
    con.execute("""
        CREATE TEMP TABLE cellstat AS
        SELECT arm, cell_grade, scid, county_fips, county_name, state,
               issuer_id, issuer_name, plan_marketing_name,
               count(*) AS roster_n,
               count(*) FILTER (WHERE dialable) AS dialable_n,
               count(*) FILTER (WHERE nameless) AS nameless_n,
               count(DISTINCT CASE WHEN dialable AND NOT nameless THEN d END)
                 AS distinct_numbers
        FROM roster2
        GROUP BY ALL
    """)
    con.execute(f"""
        CREATE TEMP TABLE chosen AS
        WITH ok AS (
          SELECT * FROM cellstat WHERE distinct_numbers >= {per_cell}
        ),
        -- Within the poor arm, an F cell is a better test than a D cell, so
        -- prefer F wherever one clears the dialable gate. Ties break on a hash
        -- rather than on cell size: ordering by size would fill every arm with
        -- the largest metro counties and quietly make this a big-city study.
        per_state AS (
          SELECT *, row_number() OVER (
                     PARTITION BY arm, state
                     ORDER BY (cell_grade = 'F') DESC, hash(scid)) AS sr
          FROM ok
        ),
        ranked AS (
          SELECT *, row_number() OVER (
                     PARTITION BY arm
                     ORDER BY (cell_grade = 'F') DESC, hash(scid || state)) AS ar
          FROM per_state WHERE sr = 1
        )
        SELECT * FROM ranked WHERE ar <= {cells_per_grade}
    """)

    con.execute(f"""
        COPY (
          SELECT arm, cell_grade, scid, county_name, state, issuer_name,
                 plan_marketing_name, roster_n, dialable_n, nameless_n,
                 distinct_numbers,
                 round(100.0 * dialable_n / roster_n, 1) AS dialable_pct,
                 (scid || county_fips) IN (
                   SELECT scid || county_fips FROM chosen) AS selected
          FROM cellstat ORDER BY arm, selected DESC, distinct_numbers DESC
        ) TO '{out_dir / "sample_summary.csv"}' (FORMAT CSV, HEADER)
    """)

    dest = out_dir / "callsheet.csv"
    con.execute(f"""
        COPY (
          WITH r AS (
            -- Nameless listings are a real defect, but you cannot ask a
            -- receptionist for a provider with no name, so they are counted in
            -- sample_summary.csv (nameless_n) instead of dialed.
            SELECT r.* FROM roster2 r
            JOIN chosen c ON c.scid = r.scid AND c.county_fips = r.county_fips
            WHERE r.dialable AND NOT r.nameless
          ),
          one_per_number_in_cell AS (
            SELECT *, row_number() OVER (
                       PARTITION BY scid, county_fips, d
                       ORDER BY hash(npi::VARCHAR || d)) AS pr
            FROM r
          ),
          one_per_number_overall AS (
            SELECT *, row_number() OVER (
                       PARTITION BY d ORDER BY hash(npi::VARCHAR || scid)) AS gr
            FROM one_per_number_in_cell WHERE pr = 1
          ),
          one_per_provider AS (
            SELECT *, row_number() OVER (
                       PARTITION BY npi ORDER BY hash(d || scid)) AS nr
            FROM one_per_number_overall WHERE gr = 1
          ),
          pick AS (
            SELECT *, row_number() OVER (
                       PARTITION BY scid, county_fips
                       ORDER BY hash(npi::VARCHAR || d || scid)) AS rk
            FROM one_per_provider WHERE nr = 1
          )
          SELECT row_number() OVER (ORDER BY arm, state, scid, rk) AS study_id,
                 arm, cell_grade, plan_marketing_name AS plan, scid AS plan_id,
                 issuer_name, county_name, state,
                 provider_name, specialties, accepting AS file_says_accepting,
                 phone, toll_free AS shared_toll_free_number,
                 address, city, addr_state, zip, npi,
                 '' AS call_date, '' AS outcome, '' AS carriers_named,
                 '' AS appointment_days, '' AS notes
          FROM pick WHERE rk <= {per_cell}
          ORDER BY arm, state, scid, rk
        ) TO '{dest}' (FORMAT CSV, HEADER)
    """)

    stats = con.execute("""
        SELECT arm, count(*) AS cells, sum(distinct_numbers) FROM chosen GROUP BY arm
    """).fetchall()
    n = con.execute(f"SELECT count(*) FROM '{dest}'").fetchone()[0]
    numbers, offices, issuers, states = con.execute(f"""
        SELECT count(*), count(DISTINCT phone), count(DISTINCT issuer_name),
               count(DISTINCT state) FROM '{dest}'
    """).fetchone()
    for arm, cells, _ in stats:
        if cells < cells_per_grade:
            log.warning(
                "callsheet: arm %s got %d of %d cells; fewer cells hold %d "
                "distinct dialable numbers than requested",
                arm, cells, cells_per_grade, per_cell,
            )
    (out_dir / "PROTOCOL.txt").write_text(
        PROTOCOL.format(cells=cells_per_grade, per_cell=per_cell)
    )
    log.info(
        "callsheet: %d call rows, %d distinct numbers, %d issuers, %d states -> %s",
        n, offices, issuers, states, dest,
    )
    return dest
