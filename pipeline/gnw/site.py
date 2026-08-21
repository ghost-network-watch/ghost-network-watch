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

log = logging.getLogger("gnw.site")

GRADE_ORDER = {"A": 0, "B": 1, "C": 2, "D": 3, "F": 4, None: 5}
METRIC_LABELS = {
    "M3_PLACEHOLDER_VALUE": "Placeholder contact values",
    "M4_STALE_ATTESTATION": "Stale attestation dates",
    "M5_CALL_CENTER_ONLY": "Single shared phone number only",
    "M6_ADDRESS_INFLATION": "Address inflation",
    "M7_OUT_OF_AREA_LISTING": "Listings outside the plan's service area",
    "M8_ACCEPTING_UNKNOWN": "Accepting-patients field missing",
    "M9_NPI_REGISTRY_STATUS": "NPI registry disagreements",
    "M10_TAXONOMY_MISMATCH": "Specialty vs registry taxonomy disagreements",
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
    return con


def _national(con) -> dict:
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
    ooa = con.execute(
        "SELECT round(100 * avg(out_of_area_rate), 1) FROM scores WHERE scope='all' AND grade IS NOT NULL"
    ).fetchone()[0]
    return {
        "grades": g,
        "plans": stats[0], "counties": stats[1], "states": stats[2],
        "thin_bh": stats[3], "bh_cells": stats[4],
        "thin_bh_pct": round(100 * stats[3] / max(stats[4], 1), 1),
        "records": recs[0], "npis": recs[1],
        "deactivated": deact, "avg_ooa_pct": ooa,
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


def _county_rows(con) -> dict[str, dict]:
    """county_fips -> {name, state, plans: [row...]} (both scopes folded)."""
    counties: dict[str, dict] = {}
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
        c = counties.setdefault(
            r[0], {"fips": r[0], "name": r[1], "state": r[2], "plans": []}
        )
        c["plans"].append(
            {
                "scid": r[3], "issuer_id": r[3][:5], "issuer": r[4] or "Unknown issuer",
                "plan": r[5] or r[3], "metal": r[6],
                "bh_grade": r[7], "bh_score": r[8], "bh_n": r[9],
                "bh_thin": bool(r[10]), "bh_low": bool(r[11]),
                "all_grade": r[12], "all_score": r[13], "all_n": r[14],
                "ooa_pct": round(100 * (r[15] or 0), 1),
            }
        )
    for c in counties.values():
        c["plans"].sort(key=lambda p: (GRADE_ORDER.get(p["bh_grade"], 5), -(p["bh_n"] or 0)))
    return counties


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


def _write_exports(con, out_data: Path, snapshot: str) -> list[dict]:
    out_data.mkdir(parents=True, exist_ok=True)
    exports = []

    def copy_query(name: str, sql: str, note: str) -> None:
        dest = out_data / name
        con.execute(f"COPY ({sql}) TO '{dest}' (FORMAT CSV, HEADER, COMPRESSION GZIP)")
        exports.append({"file": name, "note": note, "mb": round(dest.stat().st_size / 1e6, 1)})

    copy_query(
        "plan_county_scores.csv.gz", "SELECT * FROM scores",
        "Every plan x county x scope cell: scores, components, grades, sample sizes.",
    )
    copy_query(
        "thin_rosters_bh.csv.gz",
        "SELECT scid, county_fips, county_name, state, issuer_name, plan_marketing_name, n "
        "FROM scores WHERE scope='bh' AND thin_roster ORDER BY state, county_fips",
        "Behavioral-health cells listing fewer than 10 providers: the roster fact itself.",
    )
    copy_query(
        "feed_status.csv.gz", "SELECT * FROM feed_flags",
        "Feed-level findings: unreachable mandated URLs and browser-only access.",
    )
    full = {
        "M3_PLACEHOLDER_VALUE": "All placeholder-value evidence rows.",
        "M8_ACCEPTING_UNKNOWN": "All accepting-unknown evidence rows.",
        "M9_NPI_REGISTRY_STATUS": "All NPI registry evidence rows.",
        "M10_TAXONOMY_MISMATCH": "All taxonomy-disagreement evidence rows.",
    }
    for m, note in full.items():
        copy_query(f"{m}.csv.gz", f"SELECT * FROM {m.lower()}", note)
    for m, cap in (
        ("M4_STALE_ATTESTATION", 250_000),
        ("M5_CALL_CENTER_ONLY", 250_000),
        ("M6_ADDRESS_INFLATION", 250_000),
    ):
        total = con.execute(f"SELECT count(*) FROM {m.lower()}").fetchone()[0]
        copy_query(
            f"{m}.sample.csv.gz",
            f"SELECT * FROM {m.lower()} USING SAMPLE {min(cap, total)} ROWS",
            f"Random sample of {min(cap, total):,} of {total:,} evidence rows "
            "(full set reproducible from the pipeline).",
        )
    total7 = con.execute("SELECT count(*) FROM m7_out_of_area_listing").fetchone()[0]
    copy_query(
        "M7_OUT_OF_AREA_by_plan.csv.gz",
        "SELECT plan_id AS scid, count(*) AS out_of_area_attachments "
        "FROM m7_out_of_area_listing GROUP BY 1 ORDER BY 2 DESC",
        f"Out-of-area attachment counts per plan (from {total7:,} attachment-level rows; "
        "record-level sample below).",
    )
    copy_query(
        "M7_OUT_OF_AREA_LISTING.sample.csv.gz",
        "SELECT * FROM m7_out_of_area_listing USING SAMPLE 250000 ROWS",
        f"Random sample of 250,000 of {total7:,} out-of-area evidence rows.",
    )
    return exports


def build_site(
    data_root: Path,
    snapshot: str,
    repo_root: Path,
    out_dir: Path,
    wa_kit: Path,
) -> Path:
    con = _con(data_root, snapshot)
    env = Environment(
        loader=FileSystemLoader(repo_root / "site" / "templates"),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    national = _national(con)
    issuer_names = _issuer_names(con)
    counties = _county_rows(con)
    issuers = _issuer_pages(con, issuer_names)

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
    worst_grade_name = {0: "A", 1: "B", 2: "C", 3: "D", 4: "F", 5: None}

    league = con.execute("""
        SELECT substr(scid, 1, 5) AS iid, any_value(issuer_name), count(*) AS cells,
               round(avg(score), 1) AS avg_score,
               round(100 * avg(out_of_area_rate), 1) AS ooa,
               string_agg(DISTINCT state, ', ' ORDER BY state) AS states
        FROM scores WHERE scope='bh' AND grade IS NOT NULL
        GROUP BY 1 HAVING count(*) >= 50 ORDER BY avg_score ASC LIMIT 15
    """).fetchall()

    base_ctx = {
        "snapshot": snapshot,
        "site_name": "Ghost Network Watch",
        "contact": "contact@ghostnetworkwatch.org",
    }

    def render(template: str, dest: Path, **ctx) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_text(env.get_template(template).render(**base_ctx, **ctx))

    render(
        "index.html", out_dir / "index.html",
        national=national, league=league,
        states=sorted(
            (
                {
                    "code": code, "counties": len(s["counties"]),
                    "issuers": len(s["issuers"]),
                    "thin": sum(c["thin"] for c in s["counties"]),
                }
                for code, s in states.items()
            ),
            key=lambda x: x["code"],
        ),
        depth="",
    )
    for code, s in states.items():
        render(
            "state.html", out_dir / "states" / code / "index.html",
            code=code, state=s, worst_grade_name=worst_grade_name, depth="../../",
        )
    for c in counties.values():
        render(
            "county.html", out_dir / "counties" / c["fips"] / "index.html",
            county=c, depth="../../",
        )
    for i in issuers.values():
        render(
            "issuer.html", out_dir / "issuers" / i["id"] / "index.html",
            issuer=i, depth="../../",
        )
    render("methodology.html", out_dir / "methodology" / "index.html", depth="../")
    render("about.html", out_dir / "about" / "index.html", depth="../")

    exports = _write_exports(con, out_dir / "data" / "files", snapshot)
    render("data.html", out_dir / "data" / "index.html", exports=exports, depth="../")

    shutil.copy(repo_root / "site" / "assets" / "brand.css", out_dir / "brand.css")
    if not (out_dir / "webawesome").exists():
        shutil.copytree(wa_kit, out_dir / "webawesome")
    log.info(
        "site: %d county pages, %d issuer pages, %d state pages -> %s",
        len(counties), len(issuers), len(states), out_dir,
    )
    return out_dir
