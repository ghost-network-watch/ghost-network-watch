"""Static site generator: scores + evidence -> ghostnetworkwatch.org.

Reads the snapshot's score table, flag evidence, and manifest; emits a fully
static site (Web Awesome components, token-only styling) plus the open-data
downloads. No server, no accounts, no PHI: aggregates on pages, evidence in
downloadable files.

Page inventory:
  /                     national summary
  /states/XX/           per-state county list
  /counties/FIPS/       per-county plan table (the core page)
  /issuers/NNNNN/       per-issuer rollup + feed status + metric mix
  /methodology/         adapted rubric
  /data/                downloads
  /about/               who, why, correction channel
"""

from __future__ import annotations

import gzip
import json
import logging
import shutil
from collections import defaultdict
from pathlib import Path

import duckdb
from jinja2 import Environment, FileSystemLoader, select_autoescape

from .corrections import by_issuer, load_corrections

log = logging.getLogger("gnw.site")

GRADE_ORDER = {"A": 0, "B": 1, "C": 2, "D": 3, "F": 4, None: 5}
METRIC_LABELS = {
    "M3_PLACEHOLDER_VALUE": "Placeholder contact values",
    "M4_STALE_ATTESTATION": "Old update dates",
    "M5_CALL_CENTER_ONLY": "Single shared phone number only",
    "M6_ADDRESS_INFLATION": "Provider listed at many addresses",
    "M7_OUT_OF_AREA_LISTING": "Listings outside the plan's service area",
    "M8_ACCEPTING_UNKNOWN": "Accepting-patients field missing",
    "M9_NPI_REGISTRY_STATUS": "NPI registry disagreements",
    "M10_TAXONOMY_MISMATCH": "Specialty disagrees with the federal registry",
}


def _con(data_root: Path, snapshot: str) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("SET memory_limit='8GB'")
    con.execute("SET preserve_insertion_order=false")
    con.execute(f"SET temp_directory='{data_root / 'tmp' / 'duckdb'}'")
    con.execute(
        "CREATE VIEW scores AS SELECT * FROM read_parquet("
        f"'{data_root / 'scores' / snapshot / 'plan_county_scores.parquet'}')"
    )
    for m in METRIC_LABELS:
        con.execute(
            f"CREATE VIEW {m.lower()} AS SELECT * FROM read_parquet("
            f"'{data_root / 'flags' / snapshot / (m + '.parquet')}')"
        )
    con.execute(
        "CREATE VIEW feed_flags AS SELECT * FROM read_parquet("
        f"'{data_root / 'flags' / snapshot / 'feed_flags.parquet'}')"
    )
    con.execute(
        "CREATE VIEW manifest AS SELECT * FROM read_json_auto("
        f"'{data_root / 'snapshots' / snapshot / 'manifest.jsonl'}')"
    )
    con.execute(
        "CREATE VIEW providers AS SELECT * FROM read_parquet("
        f"'{data_root / 'parquet' / snapshot / 'providers' / '*.parquet'}')"
    )
    con.execute(
        "CREATE VIEW addresses AS SELECT * FROM read_parquet("
        f"'{data_root / 'parquet' / snapshot / 'provider_addresses' / '*.parquet'}')"
    )
    con.execute(
        "CREATE VIEW plan_county AS SELECT * FROM read_parquet("
        f"'{data_root / 'reference' / 'parquet' / 'plan_county.parquet'}')"
    )
    return con


# Classic 11-column US tile cartogram (state -> row, col).
TILE_GRID = {
    "AK": (1, 1), "ME": (1, 11),
    "VT": (2, 10), "NH": (2, 11),
    "WA": (3, 1), "ID": (3, 2), "MT": (3, 3), "ND": (3, 4), "MN": (3, 5),
    "WI": (3, 6), "MI": (3, 8), "NY": (3, 9), "MA": (3, 10), "RI": (3, 11),
    "OR": (4, 1), "NV": (4, 2), "WY": (4, 3), "SD": (4, 4), "IA": (4, 5),
    "IL": (4, 6), "IN": (4, 7), "OH": (4, 8), "PA": (4, 9), "NJ": (4, 10), "CT": (4, 11),
    "CA": (5, 1), "UT": (5, 2), "CO": (5, 3), "NE": (5, 4), "MO": (5, 5),
    "KY": (5, 6), "WV": (5, 7), "VA": (5, 8), "MD": (5, 9), "DE": (5, 10),
    "AZ": (6, 2), "NM": (6, 3), "KS": (6, 4), "AR": (6, 5), "TN": (6, 6),
    "NC": (6, 7), "SC": (6, 8), "DC": (6, 9),
    "OK": (7, 4), "LA": (7, 5), "MS": (7, 6), "AL": (7, 7), "GA": (7, 8),
    "HI": (8, 1), "TX": (8, 4), "FL": (8, 9),
}


def _band(score: float | None) -> str | None:
    if score is None:
        return None
    if score >= 90:
        return "A"
    if score >= 80:
        return "B"
    if score >= 70:
        return "C"
    if score >= 55:
        return "D"
    return "F"


def _state_map(con) -> list[dict]:
    agg = {
        st: {"avg": avg, "cells": cells, "thin": thin, "counties": counties}
        for st, avg, cells, thin, counties in con.execute("""
            SELECT state, round(avg(score) FILTER (grade IS NOT NULL), 1),
                   count(*) FILTER (grade IS NOT NULL),
                   count(*) FILTER (thin_roster),
                   count(DISTINCT county_fips)
            FROM scores WHERE scope='bh' GROUP BY 1
        """).fetchall()
    }
    tiles = []
    for st, (row, col) in TILE_GRID.items():
        a = agg.get(st)
        tiles.append(
            {
                "code": st, "row": row, "col": col,
                "covered": a is not None,
                "avg": a["avg"] if a else None,
                "band": _band(a["avg"]) if a else None,
                "counties": a["counties"] if a else 0,
                "thin": a["thin"] if a else 0,
            }
        )
    return tiles


def _receipt(con) -> dict | None:
    """One verbatim record from a mandated file for the hero exhibit.
    Name and NPI are never selected; the aggregate page shows values only."""
    # Every defect the caption asserts is guaranteed by the query.
    row = con.execute("""
        SELECT p.specialties, p.accepting, p.last_updated_on,
               a.address, a.city, a.state, a.zip, a.phone, p.source_sha256
        FROM providers p
        JOIN addresses a USING (source_sha256, record_idx)
        WHERE a.phone IN ('999999999', '9999999999', '0000000000')
          AND lower(coalesce(a.address, 'null')) IN ('null', 'n/a', '')
          AND regexp_replace(coalesce(a.zip,''), '[^0-9]', '', 'g') IN ('99999', '00000')
          AND p.last_updated_on = '1900-01-01'
          AND lower(coalesce(p.accepting, '')) = 'accepting'
          AND p.specialties ILIKE '%psych%'
        ORDER BY p.source_sha256, p.record_idx LIMIT 1
    """).fetchone()
    if row is None:
        return None
    sha = row[8]
    meta = con.execute(
        "SELECT any_value(url), any_value(fetched_at) FROM manifest WHERE sha256 = ?", [sha]
    ).fetchone()
    return {
        "specialty": row[0], "accepting": row[1], "last_updated_on": row[2],
        "address": row[3], "city": row[4], "state": row[5], "zip": row[6],
        "phone": row[7], "sha_short": sha[:12],
        "host": (meta[0] or "").split("/")[2] if meta and meta[0] else "",
        "fetched": str(meta[1] or "")[:10] if meta else "",
    }


def _unauditable(con, data_root: Path) -> dict[str, dict]:
    """Insurers whose mandated feed produced no scorable data (rubric M1: grade X).

    Detected as landscape (on-exchange medical) issuers with zero rows in the
    score table. Their plans and counties come from the landscape file so the
    absence is published, not silent."""
    import csv as _csv

    from .seed import PUF_CSV

    scored = {r[0] for r in con.execute("SELECT DISTINCT substr(scid,1,5) FROM scores").fetchall()}
    out: dict[str, dict] = {}
    rows = con.execute("""
        SELECT substr(plan_id, 1, 14) AS scid, any_value(issuer_name),
               any_value(plan_marketing_name), any_value(metal_level),
               list(DISTINCT lpad(regexp_replace(fips,'[^0-9]','','g'), 5, '0')),
               list(DISTINCT state)
        FROM plan_county GROUP BY 1
    """).fetchall()
    urls = {}
    with open(PUF_CSV) as fh:
        for r in _csv.DictReader(fh):
            urls[r["Issuer ID"]] = r["URL Submitted"]
    for scid, iname, pname, metal, fips_list, states in rows:
        iid = scid[:5]
        if iid in scored:
            continue
        entry = out.setdefault(
            iid,
            {"id": iid, "name": iname or f"Issuer {iid}", "states": set(),
             "plans": [], "url": urls.get(iid, "")},
        )
        entry["plans"].append(
            {"scid": scid, "name": pname or scid, "metal": metal, "counties": fips_list}
        )
        entry["states"].update(states)
    for e in out.values():
        e["states"] = sorted(e["states"])
        e["plans"].sort(key=lambda p: p["name"] or "")
    return out


def _threshold_sensitivity(con) -> dict:
    """E2 metrics at alternate cutoffs (rubric §4.1 requires publishing these).

    These numbers are a published invitation to re-run the checks at other
    cutoffs, so each row has to be computed the way its check is computed.
    Two bugs used to break that: the staleness reference date was hardcoded,
    which would have published August's ages forever, and the address row
    counted every record instead of applying M6's INDIVIDUAL filter and
    specialty exclusion, so the number under "10 addresses" did not match the
    rule the same page describes.
    """
    total = con.execute("SELECT count(*) FROM providers").fetchone()[0]
    ref = con.execute("SELECT max(fetched_at)::DATE FROM manifest").fetchone()[0]
    stale = {}
    for days in (90, 180, 365):
        n = con.execute(f"""
            SELECT count(*) FROM providers
            WHERE last_updated_on >= '2014-01-01'
              AND last_updated_on < (DATE '{ref}' - INTERVAL {days} DAY)::VARCHAR
        """).fetchone()[0]
        stale[days] = round(100 * n / total, 1)
    addr = {}
    for thr in (5, 10, 25):
        n = con.execute(f"""
            WITH u AS (
              SELECT source_sha256, record_idx,
                     count(DISTINCT coalesce(address,'') || '|' || coalesce(city,'') || '|' ||
                           coalesce(state,'')) AS n_addr
              FROM addresses GROUP BY 1, 2
            )
            SELECT count(*)
            FROM u JOIN providers p USING (source_sha256, record_idx)
            WHERE p.type = 'INDIVIDUAL' AND u.n_addr > {thr}
              AND NOT regexp_matches(lower(coalesce(p.specialties, '')),
                  'radiolog|patholog|anesthesiolog|emergency medicine|hospitalist')
        """).fetchone()[0]
        addr[thr] = round(100 * (n or 0) / total, 2)
    # Templated rather than written into the prose, which had gone stale.
    addr_max = con.execute("""
        WITH u AS (
          SELECT source_sha256, record_idx,
                 count(DISTINCT coalesce(address,'') || '|' || coalesce(city,'') || '|' ||
                       coalesce(state,'')) AS n_addr
          FROM addresses GROUP BY 1, 2
        )
        SELECT max(u.n_addr)
        FROM u JOIN providers p USING (source_sha256, record_idx)
        WHERE p.type = 'INDIVIDUAL'
          AND NOT regexp_matches(lower(coalesce(p.specialties, '')),
              'radiolog|patholog|anesthesiolog|emergency medicine|hospitalist')
    """).fetchone()[0]
    return {"stale": stale, "addr": addr, "ref_date": str(ref),
            "addr_max_individual": addr_max}


def _national(con, snapshot: str) -> dict:
    # Deactivation and update ages are measured against the first of the
    # snapshot month, close enough to the fetch date for a "months ago" line.
    ref = f"{snapshot}-01"
    g = {}
    for scope, grade, cells in con.execute(
        "SELECT scope, coalesce(grade, 'thin'), count(*) FROM scores GROUP BY 1, 2"
    ).fetchall():
        g.setdefault(scope, {})[grade] = cells
    stats = con.execute("""
        SELECT count(DISTINCT scid) FILTER (scope='bh'),
               count(DISTINCT county_fips) FILTER (scope='bh'),
               count(DISTINCT state) FILTER (scope='bh'),
               count(*) FILTER (scope='bh' AND thin_roster),
               count(*) FILTER (scope='bh')
        FROM scores
    """).fetchone()
    recs = con.execute("SELECT count(*), count(DISTINCT npi) FROM providers").fetchone()
    deact = con.execute(
        "SELECT count(DISTINCT npi) FROM m9_npi_registry_status WHERE subcode='DEACTIVATED'"
    ).fetchone()[0]
    # Per-plan mean (one value per plan), because the sentence on the page
    # says "of the providers listed for a plan". The cell-weighted mean is 52%.
    ooa = con.execute("""
        SELECT round(100 * avg(rate), 1) FROM (
          SELECT any_value(out_of_area_rate) AS rate
          FROM scores WHERE scope='all' AND out_of_area_rate IS NOT NULL
          GROUP BY scid
        )
    """).fetchone()[0]
    # Single worst placeholder-phone file (>= 500 records, to skip tiny files):
    # its placeholder-phone rate, paired with the share of the SAME file that
    # marks its records as accepting new patients.
    # Restrict to predominantly individual-provider directories (>= 90% of
    # records), where a placeholder phone shared across every listed clinician
    # is unambiguous. Facility files legitimately share a switchboard line.
    worst = con.execute("""
        WITH tot AS (
          SELECT source_sha256, count(*) AS recs,
                 count(*) FILTER (accepting = 'accepting') AS acc,
                 count(*) FILTER (type = 'INDIVIDUAL') AS ind
          FROM providers GROUP BY 1
        ),
        ph AS (
          SELECT source_sha256, count(DISTINCT record_idx) AS phr
          FROM m3_placeholder_value WHERE subcode = 'PHONE' GROUP BY 1
        )
        SELECT round(100.0 * ph.phr / tot.recs), round(100.0 * tot.acc / tot.recs)
        FROM tot JOIN ph USING (source_sha256)
        WHERE tot.recs >= 500 AND tot.ind >= 0.9 * tot.recs
        -- worst placeholder-phone rate first, then the largest such file, then
        -- hash, so the pick is deterministic across monthly rebuilds.
        ORDER BY 1.0 * ph.phr / tot.recs DESC, tot.recs DESC, tot.source_sha256
        LIMIT 1
    """).fetchone()
    # Median age in months between an identifier's federal deactivation date and
    # the snapshot month, over the same distinct deactivated NPIs counted above.
    deact_med = con.execute(f"""
        SELECT round(median(date_diff('month',
                 TRY_STRPTIME(observed ->> 'deactivation_date', '%m/%d/%Y'),
                 DATE '{ref}')))
        FROM (
          SELECT npi, any_value(observed) AS observed
          FROM m9_npi_registry_status WHERE subcode = 'DEACTIVATED' GROUP BY npi
        )
        WHERE TRY_STRPTIME(observed ->> 'deactivation_date', '%m/%d/%Y') IS NOT NULL
    """).fetchone()[0]
    # Directory files whose newest usable update date is more than a year before
    # the snapshot month, against the monthly-update requirement.
    stale_files = con.execute(f"""
        WITH f AS (
          SELECT source_sha256, max(TRY_CAST(last_updated_on AS DATE)) AS newest
          FROM providers GROUP BY 1
        )
        SELECT count(*) FROM f
        WHERE newest IS NOT NULL AND newest < DATE '{ref}' - INTERVAL 365 DAY
    """).fetchone()[0]
    return {
        "grades": g,
        "plans": stats[0], "counties": stats[1], "states": stats[2],
        "thin_bh": stats[3], "bh_cells": stats[4],
        "thin_bh_pct": round(100 * stats[3] / max(stats[4], 1), 1),
        "records": recs[0], "npis": recs[1],
        "deactivated": deact, "avg_ooa_pct": ooa,
        "worst_phone_pct": int(worst[0]), "worst_phone_accepting_pct": int(worst[1]),
        "deact_median_months": int(deact_med), "stale_files": stale_files,
    }


def _issuer_names(con) -> dict[str, dict]:
    """issuer_id -> {name, states} from the score table itself."""
    out: dict[str, dict] = {}
    for iid, name, states in con.execute("""
        SELECT substr(scid, 1, 5), any_value(issuer_name),
               list_sort(list_distinct(list(DISTINCT state)))
        FROM scores WHERE issuer_name IS NOT NULL GROUP BY 1
    """).fetchall():
        out[iid] = {"name": name, "states": states}
    return out


def _cells(con) -> tuple[dict[str, dict], dict[str, dict]]:
    """Fold score cells two ways:
    counties: county_fips -> {name, state, plans: [cell...]}
    plans:    scid -> {name, issuer, metal, counties: [cell...], rollups}
    """
    counties: dict[str, dict] = {}
    plans: dict[str, dict] = {}
    rows = con.execute("""
        SELECT county_fips, any_value(county_name), any_value(state), scid,
               any_value(issuer_name), any_value(plan_marketing_name), any_value(metal_level),
               max(CASE WHEN scope='bh' THEN grade END),
               max(CASE WHEN scope='bh' THEN score END),
               max(CASE WHEN scope='bh' THEN n END),
               max(CASE WHEN scope='bh' THEN thin_roster::INT END),
               max(CASE WHEN scope='bh' THEN low_sample::INT END),
               max(CASE WHEN scope='all' THEN grade END),
               max(CASE WHEN scope='all' THEN score END),
               max(CASE WHEN scope='all' THEN n END),
               max(out_of_area_rate)
        FROM scores GROUP BY county_fips, scid
    """).fetchall()
    for r in rows:
        cell = {
            "fips": r[0], "county_name": r[1], "state": r[2],
            "scid": r[3], "issuer_id": r[3][:5], "issuer": r[4] or "Unknown issuer",
            "plan": r[5] or r[3], "metal": r[6],
            "bh_grade": r[7], "bh_score": r[8], "bh_n": r[9],
            "bh_thin": bool(r[10]), "bh_low": bool(r[11]),
            "all_grade": r[12], "all_score": r[13], "all_n": r[14],
            "ooa_pct": round(100 * (r[15] or 0), 1),
        }
        c = counties.setdefault(
            r[0], {"fips": r[0], "name": r[1], "state": r[2], "plans": []}
        )
        c["plans"].append(cell)
        p = plans.setdefault(
            r[3],
            {
                "scid": r[3], "issuer_id": r[3][:5], "issuer": cell["issuer"],
                "name": cell["plan"], "metal": r[6], "ooa_pct": cell["ooa_pct"],
                "counties": [], "states": set(),
            },
        )
        p["counties"].append(cell)
        p["states"].add(r[2])
    for c in counties.values():
        c["plans"].sort(key=lambda x: (GRADE_ORDER.get(x["bh_grade"], 5), -(x["bh_n"] or 0)))
    uniform = {
        (r[0], r[1]): r[2]
        for r in con.execute(
            "SELECT scid, county_fips, score_uniform FROM scores WHERE scope='bh' AND grade IS NOT NULL"
        ).fetchall()
    }
    for p in plans.values():
        p["counties"].sort(key=lambda x: (x["state"], x["county_name"] or ""))
        p["states"] = sorted(p["states"])
        scored = [x["bh_score"] for x in p["counties"] if x["bh_score"] is not None and not x["bh_thin"]]
        p["avg_bh"] = round(sum(scored) / len(scored), 1) if scored else None
        p["band"] = _band(p["avg_bh"])
        # Rubric §3.4: when the uniform-weight check lands in a different band,
        # the plan page must show both.
        uscores = [
            uniform[(p["scid"], c["fips"])]
            for c in p["counties"]
            if (p["scid"], c["fips"]) in uniform and uniform[(p["scid"], c["fips"])] is not None
        ]
        p["avg_uniform"] = round(sum(uscores) / len(uscores), 1) if uscores else None
        p["band_uniform"] = _band(p["avg_uniform"])
        p["thin"] = sum(1 for x in p["counties"] if x["bh_thin"])
    return counties, plans


def _issuer_pages(con, issuer_names) -> dict[str, dict]:
    issuers: dict[str, dict] = {}
    for iid, meta in issuer_names.items():
        issuers[iid] = {
            "id": iid, "name": meta["name"], "states": meta["states"],
            "grades": {}, "cells": 0, "thin": 0, "avg_bh": None, "metrics": [],
            "feed": [], "hosts": [],
        }
    for iid, grade, cells in con.execute("""
        SELECT substr(scid, 1, 5), coalesce(grade, 'thin'), count(*)
        FROM scores WHERE scope='bh' GROUP BY 1, 2
    """).fetchall():
        if iid in issuers:
            issuers[iid]["grades"][grade] = cells
            issuers[iid]["cells"] += cells
    for iid, avg, thin in con.execute("""
        SELECT substr(scid, 1, 5), round(avg(score), 1),
               count(*) FILTER (thin_roster)
        FROM scores WHERE scope='bh' GROUP BY 1
    """).fetchall():
        if iid in issuers:
            issuers[iid]["avg_bh"] = avg
            issuers[iid]["thin"] = thin

    # Metric mix per issuer via manifest file attribution. Files shared by
    # multiple issuers (platform indexes) count toward each, disclosed on page.
    con.execute("""
        CREATE TEMP TABLE file_issuers AS
        SELECT DISTINCT sha256, unnest(issuer_ids) AS issuer_id,
               len(issuer_ids) > 1 AS shared
        FROM manifest WHERE sha256 IS NOT NULL AND role = 'provider'
    """)
    con.execute("""
        CREATE TEMP TABLE issuer_records AS
        SELECT fi.issuer_id, count(*) AS n_records, bool_or(fi.shared) AS any_shared
        FROM providers p JOIN file_issuers fi ON fi.sha256 = p.source_sha256
        GROUP BY 1
    """)
    for m in METRIC_LABELS:
        if m == "M7_OUT_OF_AREA_LISTING":
            continue  # attachment-grain; reported via out-of-area rate instead
        for iid, n, total, shared in con.execute(f"""
            SELECT fi.issuer_id, count(DISTINCT (f.source_sha256, f.record_idx)),
                   any_value(ir.n_records), any_value(ir.any_shared)
            FROM {m.lower()} f
            JOIN file_issuers fi ON fi.sha256 = f.source_sha256
            JOIN issuer_records ir ON ir.issuer_id = fi.issuer_id
            GROUP BY 1
        """).fetchall():
            if iid in issuers and total:
                issuers[iid]["metrics"].append(
                    {
                        "metric": m, "label": METRIC_LABELS[m],
                        "records": n, "total": total,
                        "pct": round(100 * n / total, 2), "shared": shared,
                    }
                )
    for iid, rate in con.execute("""
        SELECT substr(scid, 1, 5), round(100 * avg(out_of_area_rate), 1)
        FROM scores WHERE scope='all' AND out_of_area_rate IS NOT NULL GROUP BY 1
    """).fetchall():
        if iid in issuers:
            issuers[iid]["ooa_pct"] = rate
    for url, metric, subcode, ids in con.execute(
        "SELECT url, metric, subcode, issuer_ids FROM feed_flags"
    ).fetchall():
        for iid in ids:
            if iid in issuers:
                issuers[iid]["feed"].append({"url": url, "metric": metric, "subcode": subcode})
    for iid, hosts in con.execute("""
        SELECT iid, list_distinct(list(host))
        FROM (
          SELECT unnest(issuer_ids) AS iid,
                 regexp_extract(url, '//([^/]+)/', 1) AS host
          FROM manifest WHERE role = 'index'
        ) GROUP BY 1
    """).fetchall():
        if iid in issuers:
            issuers[iid]["hosts"] = hosts
    for i in issuers.values():
        i["metrics"].sort(key=lambda x: -x["pct"])
    return issuers


def _issuer_evidence(con, out_data: Path) -> dict[str, float]:
    """One combined evidence CSV per issuer, linked from its page.

    All record-level metrics share the evidence-row schema, so they union
    cleanly. M7 is attachment-grain and enormous; it is included as per-plan
    totals instead of raw rows. Requires the file_issuers temp table
    (created in _issuer_pages).
    """
    dest_dir = out_data / "issuers"
    dest_dir.mkdir(parents=True, exist_ok=True)
    record_metrics = [m for m in METRIC_LABELS if m != "M7_OUT_OF_AREA_LISTING"]
    union = " UNION ALL ".join(f"SELECT * FROM {m.lower()}" for m in record_metrics)
    con.execute(f"""
        CREATE TEMP TABLE issuer_evidence AS
        SELECT fi.issuer_id, f.*
        FROM ({union}) f
        JOIN file_issuers fi ON fi.sha256 = f.source_sha256
        UNION ALL
        SELECT substr(plan_id, 1, 5) AS issuer_id,
               any_value(snapshot), 'M7_OUT_OF_AREA_SUMMARY', 'PER_PLAN_TOTAL',
               'E2', 0.8, NULL, NULL, NULL, plan_id,
               to_json(struct_pack(out_of_area_attachments := count(*))),
               any_value(rule_version)
        FROM m7_out_of_area_listing GROUP BY plan_id
    """)
    sizes: dict[str, float] = {}
    for (iid,) in con.execute(
        "SELECT DISTINCT issuer_id FROM issuer_evidence ORDER BY 1"
    ).fetchall():
        dest = dest_dir / f"{iid}_evidence.csv.gz"
        con.execute(
            f"COPY (SELECT * FROM issuer_evidence WHERE issuer_id = '{iid}' "
            f"ORDER BY metric, source_sha256, record_idx) "
            f"TO '{dest}' (FORMAT CSV, HEADER, COMPRESSION GZIP)"
        )
        sizes[iid] = round(dest.stat().st_size / 1e6, 2)
    con.execute("DROP TABLE issuer_evidence")
    log.info("issuer evidence files: %d", len(sizes))
    return sizes


def _write_exports(con, out_data: Path, snapshot: str) -> list[dict]:
    out_data.mkdir(parents=True, exist_ok=True)
    exports = []

    def copy_query(name: str, sql: str, note: str, group: str) -> None:
        dest = out_data / name
        con.execute(f"COPY ({sql}) TO '{dest}' (FORMAT CSV, HEADER, COMPRESSION GZIP)")
        mb = dest.stat().st_size / 1e6
        exports.append(
            {"file": name, "note": note, "group": group,
             "mb": round(mb, 2) if mb < 1 else round(mb, 1)}
        )

    copy_query(
        "plan_county_scores.csv.gz", "SELECT * FROM scores",
        "Every plan's score and grade in every county. One spreadsheet with everything "
        "on this site. One row per plan, county, and scope (mental health or all providers).",
        "start",
    )
    copy_query(
        "thin_rosters_bh.csv.gz",
        "SELECT scid, county_fips, county_name, state, issuer_name, plan_marketing_name, n "
        "FROM scores WHERE scope='bh' AND thin_roster ORDER BY state, county_fips",
        "Every place a plan lists fewer than 10 mental health providers in a county. "
        "Plan name, county, and the count.",
        "start",
    )
    copy_query(
        "feed_status.csv.gz", "SELECT * FROM feed_flags",
        "Insurers whose required directory web address was dead, or answered only to a "
        "web browser and blocked automated tools.",
        "start",
    )
    # Provenance. Every evidence row cites a source_sha256, and this is the table
    # that says what that hash refers to: which URL, fetched when, what the
    # server said it was. Without it the hashes are unresolvable to a reader, and
    # the site's "check our work" claim has nothing behind it.
    copy_query(
        "source_manifest.csv.gz",
        "SELECT role, url, final_url, "
        "array_to_string(issuer_ids, ' ') AS issuer_ids, "
        "array_to_string(states, ' ') AS states, "
        "fetched_at, status, sha256, bytes_content, content_type, "
        "last_modified, etag, elapsed_s, error "
        "FROM manifest ORDER BY url",
        "Every file we fetched this month: its web address, the moment we fetched it, "
        "the HTTP status, the SHA-256 of the bytes we stored, and the ETag and "
        "Last-Modified the server itself reported. This is what the source_sha256 in "
        "every evidence row refers to.",
        "start",
    )
    full = {
        "M3_PLACEHOLDER_VALUE": "Every listing with placeholder contact data: phones like "
        "999999999, ZIP 99999, addresses reading 'null', dates before 2014.",
        "M8_ACCEPTING_UNKNOWN": "Every individual-provider listing that leaves the required "
        "accepting-new-patients field blank or unknown.",
        "M9_NPI_REGISTRY_STATUS": "Every listing whose provider identifier is malformed or "
        "deactivated in the federal registry.",
        "M10_TAXONOMY_MISMATCH": "Every listing labeled mental health whose federal registry "
        "record shows only unrelated specialties.",
    }
    for m, note in full.items():
        copy_query(f"{m}.csv.gz", f"SELECT * FROM {m.lower()}", note + " Complete set.", "evidence")
    for m, note in (
        ("M4_STALE_ATTESTATION", "Listings whose own last-updated date is more than 180 days old."),
        ("M5_CALL_CENTER_ONLY", "Listings whose only phone number is one appearing on at least "
         "1% of the file's listings or 50 listings, whichever is larger."),
        ("M6_ADDRESS_INFLATION", "Individual providers listed at more than 10 street addresses "
         "at once (organizations, 25)."),
    ):
        total = con.execute(f"SELECT count(*) FROM {m.lower()}").fetchone()[0]
        cap = min(250_000, total)
        if cap >= total:
            sample_note = f"{note} Complete set, {total:,} rows."
        else:
            sample_note = (
                f"{note} The complete set is {total:,} rows; this file is a random "
                f"sample of {cap:,}. See below for how to rebuild the complete set."
            )
        # Name the file for what it holds. These three metrics are usually over
        # the cap, but not always, and a complete set shipped under a ".sample"
        # filename invites a reader to think rows were withheld.
        copy_query(
            f"{m}.csv.gz" if cap >= total else f"{m}.sample.csv.gz",
            f"SELECT * FROM {m.lower()}" if cap >= total
            else f"SELECT * FROM {m.lower()} USING SAMPLE {cap} ROWS",
            sample_note,
            "evidence",
        )
    total7 = con.execute("SELECT count(*) FROM m7_out_of_area_listing").fetchone()[0]
    copy_query(
        "M7_OUT_OF_AREA_by_plan.csv.gz",
        "SELECT plan_id AS scid, count(*) AS out_of_area_attachments "
        "FROM m7_out_of_area_listing GROUP BY 1 ORDER BY 2 DESC",
        "For each plan, how many of its listings have no address inside the plan's filed "
        "service area or any neighboring county. Totals per plan.",
        "evidence",
    )
    copy_query(
        "M7_OUT_OF_AREA_LISTING.sample.csv.gz",
        "SELECT * FROM m7_out_of_area_listing USING SAMPLE 250000 ROWS",
        f"The listing-level rows behind the per-plan totals. The complete set is "
        f"{total7:,} rows; this file is a random sample of 250,000.",
        "evidence",
    )
    return exports


def _base_ctx(snapshot: str, css_file: str) -> dict:
    year, month = int(snapshot[:4]), int(snapshot[5:7])
    ny, nm = (year + 1, 1) if month == 12 else (year, month + 1)
    names = ["", "January", "February", "March", "April", "May", "June", "July",
             "August", "September", "October", "November", "December"]
    return {
        "snapshot": snapshot,
        "site_name": "Ghost Network Watch",
        "contact": "contact@ghostnetworkwatch.org",
        "css_file": css_file,
        "snapshot_label": f"{names[month]} {year}",
        "next_update": f"early {names[nm]} {ny}",
        "launch_date": "October 26, 2026",
        "prelaunch": False,
        "repo_url": "https://github.com/ghost-network-watch/ghost-network-watch",
    }


def _hash_css(repo_root: Path, out_dir: Path) -> str:
    import hashlib

    css_src = (repo_root / "site" / "assets" / "brand.css").read_bytes()
    css_file = f"brand.{hashlib.sha256(css_src).hexdigest()[:10]}.css"
    (out_dir / css_file).write_bytes(css_src)
    for old in out_dir.glob("brand.*.css"):
        if old.name != css_file:
            old.unlink()
    return css_file


def _build_prelaunch(con, snapshot, repo_root, out_dir, wa_kit, env) -> Path:
    """Pre-launch site: only pages that name no insurer. Publishing the
    methodology before any finding is deliberate; findings wait for the
    issuer notification window."""
    css_file = _hash_css(repo_root, out_dir)
    base = _base_ctx(snapshot, css_file)
    base["prelaunch"] = True
    thresholds = _threshold_sensitivity(con)

    def render(template: str, dest: Path, **ctx) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(env.get_template(template).render(**base, **ctx))

    render("index_prelaunch.html", out_dir / "index.html", depth="")
    render("methodology.html", out_dir / "methodology" / "index.html",
           depth="../", nav="methodology", thresholds=thresholds)
    render("patients.html", out_dir / "patients" / "index.html", depth="../", nav="patients")
    render("about.html", out_dir / "about" / "index.html", depth="../", nav="about")
    # Published before the first finding, and empty if nothing is disputed, so a
    # reader can distinguish "nobody disputed this" from "disputes go unpublished".
    render("corrections.html", out_dir / "corrections" / "index.html",
           depth="../", nav="corrections", corrections=load_corrections(repo_root))
    render("404.html", out_dir / "404.html", depth="/")

    base_url = "https://ghostnetworkwatch.org"
    urls = ["", "patients/", "methodology/", "about/", "corrections/"]
    (out_dir / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(f"<url><loc>{base_url}/{u}</loc></url>" for u in urls)
        + "\n</urlset>\n"
    )
    (out_dir / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\nSitemap: {base_url}/sitemap.xml\n"
    )
    shutil.copytree(
        repo_root / "site" / "assets" / "fonts", out_dir / "fonts", dirs_exist_ok=True
    )
    shutil.copy(repo_root / "site" / "assets" / "sort.js", out_dir / "sort.js")
    if not (out_dir / "webawesome").exists():
        shutil.copytree(wa_kit, out_dir / "webawesome")
    log.info("prelaunch site: 5 pages -> %s", out_dir)
    return out_dir


def build_site(
    data_root: Path,
    snapshot: str,
    repo_root: Path,
    out_dir: Path,
    wa_kit: Path,
    prelaunch: bool = False,
) -> Path:
    con = _con(data_root, snapshot)
    env = Environment(
        loader=FileSystemLoader(repo_root / "site" / "templates"),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    if prelaunch:
        return _build_prelaunch(con, snapshot, repo_root, out_dir, wa_kit, env)

    national = _national(con, snapshot)
    issuer_names = _issuer_names(con)
    counties, plans = _cells(con)
    issuers = _issuer_pages(con, issuer_names)
    unauditable = _unauditable(con, data_root)
    thresholds = _threshold_sensitivity(con)
    corrections = load_corrections(repo_root)
    corrections_by_issuer = by_issuer(corrections)

    # Unauditable insurers' plans appear on county pages as grade X rows.
    fips_names = {c["fips"]: (c["name"], c["state"]) for c in counties.values()}
    for u in unauditable.values():
        for pl in u["plans"]:
            for fips in pl["counties"]:
                if fips not in counties:
                    continue
                counties[fips]["plans"].append(
                    {
                        "fips": fips, "county_name": fips_names[fips][0],
                        "state": fips_names[fips][1],
                        "scid": pl["scid"], "issuer_id": u["id"], "issuer": u["name"],
                        "plan": pl["name"], "metal": pl["metal"],
                        "bh_grade": "X", "bh_score": None, "bh_n": None,
                        "bh_thin": False, "bh_low": False,
                        "all_grade": "X", "all_score": None, "all_n": None,
                        "ooa_pct": None, "unauditable": True,
                    }
                )

    # ZIP search index: zip -> covered county fips list, plus fips -> label.
    search_dir = out_dir / "search"
    search_dir.mkdir(parents=True, exist_ok=True)
    zip_map: dict[str, list[str]] = {}
    for z, fips in con.execute(
        "SELECT zip, county_fips FROM read_parquet(?)",
        [str(data_root / "reference" / "parquet" / "zip_county.parquet")],
    ).fetchall():
        if fips in counties:
            zip_map.setdefault(z, []).append(fips)
    (search_dir / "zips.json").write_text(json.dumps(zip_map, separators=(",", ":")))
    (search_dir / "counties.json").write_text(
        json.dumps(
            {f: f"{c['name']}, {c['state']}" for f, c in counties.items()},
            separators=(",", ":"),
        )
    )

    states: dict[str, dict] = defaultdict(lambda: {"counties": [], "issuers": set()})
    for c in counties.values():
        states[c["state"]]["counties"].append(
            {
                "fips": c["fips"], "name": c["name"],
                "plans": len(c["plans"]),
                "worst": min((GRADE_ORDER.get(p["bh_grade"], 5) for p in c["plans"]), default=5),
                "thin": sum(1 for p in c["plans"] if p["bh_thin"]),
            }
        )
        for p in c["plans"]:
            states[c["state"]]["issuers"].add(p["issuer"])
    for s in states.values():
        s["counties"].sort(key=lambda x: (x["name"] or ""))
        s["issuers"] = sorted(s["issuers"])
    state_stats = {
        r[0]: {"avg": r[1], "band": _band(r[1]), "grades": {}, "thin": r[2], "df": r[3]}
        for r in con.execute("""
            SELECT state, round(avg(score) FILTER (grade IS NOT NULL), 1),
                   count(*) FILTER (thin_roster),
                   count(*) FILTER (grade IN ('D','F'))
            FROM scores WHERE scope='bh' GROUP BY 1
        """).fetchall()
    }
    for st, grade, n in con.execute(
        "SELECT state, coalesce(grade,'thin'), count(*) FROM scores WHERE scope='bh' GROUP BY 1,2"
    ).fetchall():
        if st in state_stats:
            state_stats[st]["grades"][grade] = n
    for u in unauditable.values():
        for st in u["states"]:
            if st in state_stats:
                state_stats[st].setdefault("unauditable", []).append(
                    {"id": u["id"], "name": u["name"]}
                )
    worst_grade_name = {0: "A", 1: "B", 2: "C", 3: "D", 4: "F", 5: None}

    # Rubric §3.5: rankings exclude low-sample cohorts.
    league = con.execute("""
        SELECT substr(scid, 1, 5) AS iid, any_value(issuer_name), count(*) AS cells,
               round(avg(score), 1) AS avg_score,
               round(100 * avg(out_of_area_rate), 1) AS ooa,
               string_agg(DISTINCT state, ', ' ORDER BY state) AS states
        FROM scores WHERE scope='bh' AND grade IS NOT NULL AND NOT low_sample
        GROUP BY 1 HAVING count(*) >= 50 ORDER BY avg_score ASC LIMIT 15
    """).fetchall()

    # Content-hashed stylesheet name: rebuilds bust browser and CDN caches.
    css_file = _hash_css(repo_root, out_dir)
    base_ctx = _base_ctx(snapshot, css_file)
    month_names = ["", "January", "February", "March", "April", "May", "June", "July",
                   "August", "September", "October", "November", "December"]

    def render(template: str, dest: Path, **ctx) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(env.get_template(template).render(**base_ctx, **ctx))

    render(
        "index.html", out_dir / "index.html",
        national=national, league=league,
        tiles=_state_map(con), receipt=_receipt(con),
        unauditable=sorted(unauditable.values(), key=lambda u: u["name"]),
        depth="",
    )
    for code, s in states.items():
        render(
            "state.html", out_dir / "states" / code / "index.html",
            code=code, state=s, stats=state_stats.get(code, {}),
            worst_grade_name=worst_grade_name, depth="../../",
        )
    for c in counties.values():
        render(
            "county.html", out_dir / "counties" / c["fips"] / "index.html",
            county=c, depth="../../",
        )
    evidence_sizes = _issuer_evidence(con, out_dir / "data" / "files")
    plans_by_issuer: dict[str, list] = defaultdict(list)
    for p in plans.values():
        plans_by_issuer[p["issuer_id"]].append(p)
    for lst in plans_by_issuer.values():
        lst.sort(key=lambda p: (GRADE_ORDER.get(p["band"], 5), p["name"] or ""))
    for i in issuers.values():
        render(
            "issuer.html", out_dir / "issuers" / i["id"] / "index.html",
            issuer=i, evidence_mb=evidence_sizes.get(i["id"]),
            issuer_plans=plans_by_issuer.get(i["id"], []), depth="../../",
            issuer_corrections=corrections_by_issuer.get(i["id"], []),
        )
    for u in unauditable.values():
        render(
            "issuer_unauditable.html", out_dir / "issuers" / u["id"] / "index.html",
            issuer=u, depth="../../",
        )
    for p in plans.values():
        # Complaint-ready sentences, assembled here so grammar and pluralization
        # are right and the source is an absolute URL that survives copy-paste.
        graded = [c for c in p["counties"] if c["bh_grade"] and not c["bh_thin"]]
        df = sum(1 for c in graded if c["bh_grade"] in ("D", "F"))

        def _counties(n: int) -> str:
            return "1 county" if n == 1 else f"{n} counties"

        sentences = []
        if df:
            sentences.append(
                f"The mental health directory for plan {p['name']} ({p['scid']}) was "
                f"graded D or F in {df} of its {_counties(len(graded))}."
            )
        if p["thin"]:
            subject = "It" if sentences else (
                f"The mental health directory for plan {p['name']} ({p['scid']})"
            )
            sentences.append(
                f"{subject} lists fewer than 10 mental health providers in "
                f"{_counties(p['thin'])}."
            )
        if p["ooa_pct"] and p["ooa_pct"] > 10:
            sentences.append(
                f"{p['ooa_pct']}% of the providers listed for this plan have no listed "
                "address in or near any county the plan covers."
            )
        p["complaint"] = {
            "sentences": sentences,
            "url": f"https://ghostnetworkwatch.org/plans/{p['scid']}/",
            "evidence_mb": evidence_sizes.get(p["issuer_id"]),
        }
        render(
            "plan.html", out_dir / "plans" / p["scid"] / "index.html",
            plan=p, depth="../../",
        )
    # Month-over-month changes page + RSS feed from the diff outputs.
    diff_dir = data_root / "diff" / snapshot
    diff_summary = {"first_snapshot": True}
    if (diff_dir / "summary.json").exists():
        diff_summary = json.loads((diff_dir / "summary.json").read_text())
    resolvers = [
        {**r, "name": issuer_names.get(r["issuer_id"], {}).get("name", f"Issuer {r['issuer_id']}")}
        for r in diff_summary.get("top_resolvers", [])
    ]
    if (diff_dir / "resolved_flags.csv.gz").exists():
        (out_dir / "data" / "files").mkdir(parents=True, exist_ok=True)
        shutil.copy(diff_dir / "resolved_flags.csv.gz",
                    out_dir / "data" / "files" / "resolved_flags.csv.gz")
    render("changes.html", out_dir / "changes" / "index.html", depth="../",
           nav="changes", diff=diff_summary, resolvers=resolvers)

    items = []
    for d in sorted((data_root / "diff").glob("*/summary.json"), reverse=True):
        s = json.loads(d.read_text())
        snap = s["snapshot"]
        label = f"{month_names[int(snap[5:7])]} {snap[:4]}"
        if s.get("first_snapshot"):
            desc = f"First snapshot: baseline published for {label}."
        else:
            desc = (f"{s['resolved']:,} findings resolved, {s['new']:,} new, "
                    f"{s['grades_improved']:,} grades improved and "
                    f"{s['grades_declined']:,} declined vs {s['previous']}.")
        items.append(
            f"<item><title>Ghost Network Watch: {label} snapshot</title>"
            f"<link>https://ghostnetworkwatch.org/changes/</link>"
            f"<guid isPermaLink=\"false\">gnw-{snap}</guid>"
            f"<pubDate>15 {month_names[int(snap[5:7])][:3]} {snap[:4]} 12:00:00 GMT</pubDate>"
            f"<description>{desc}</description></item>"
        )
    (out_dir / "changes").mkdir(parents=True, exist_ok=True)
    (out_dir / "changes" / "feed.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<rss version="2.0"><channel>'
        "<title>Ghost Network Watch updates</title>"
        "<link>https://ghostnetworkwatch.org/changes/</link>"
        "<description>Monthly directory integrity updates for federal marketplace plans"
        "</description>" + "".join(items) + "</channel></rss>"
    )

    render("corrections.html", out_dir / "corrections" / "index.html", depth="../",
           nav="corrections", corrections=corrections)
    render("methodology.html", out_dir / "methodology" / "index.html", depth="../",
           nav="methodology", thresholds=thresholds)
    render("patients.html", out_dir / "patients" / "index.html", depth="../", nav="patients")
    render("about.html", out_dir / "about" / "index.html", depth="../", nav="about")
    render("404.html", out_dir / "404.html", depth="/")

    exports = _write_exports(con, out_dir / "data" / "files", snapshot)
    render("data.html", out_dir / "data" / "index.html", exports=exports, depth="../", nav="data")

    # Sitemap + robots: 6,200 pages that should rank for plan-name searches.
    base_url = "https://ghostnetworkwatch.org"
    lastmod = f"{snapshot}-15"
    urls = ["", "changes/", "patients/", "methodology/", "data/", "about/"]
    urls += [f"states/{code}/" for code in states]
    urls += [f"counties/{fips}/" for fips in counties]
    urls += [f"plans/{scid}/" for scid in plans]
    urls += [f"issuers/{iid}/" for iid in issuers]
    urls += [f"issuers/{iid}/" for iid in unauditable if iid not in issuers]
    (out_dir / "sitemap.xml").write_text(
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(
            f"<url><loc>{base_url}/{u}</loc><lastmod>{lastmod}</lastmod></url>"
            for u in urls
        )
        + "\n</urlset>\n"
    )
    (out_dir / "robots.txt").write_text(
        f"User-agent: *\nAllow: /\nSitemap: {base_url}/sitemap.xml\n"
    )

    shutil.copytree(
        repo_root / "site" / "assets" / "fonts", out_dir / "fonts", dirs_exist_ok=True
    )
    shutil.copy(repo_root / "site" / "assets" / "sort.js", out_dir / "sort.js")
    if not (out_dir / "webawesome").exists():
        shutil.copytree(wa_kit, out_dir / "webawesome")
    log.info(
        "site: %d county, %d plan, %d issuer, %d state pages -> %s",
        len(counties), len(plans), len(issuers), len(states), out_dir,
    )
    return out_dir
