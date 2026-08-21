"""Flag engine: rubric metrics -> evidence rows.

Implements scoring_rubric_v0.md (scoping/evidence/). Every flag is born as an
evidence row addressing the exact payer-published bytes behind it:
(snapshot, metric, subcode, source_sha256, record_idx, npi, plan_id,
 observed JSON, evidence_strength, weight, rule_version). The score, the
drill-down UI, and issuer notifications are all views over these rows.

Record-level metrics write data/flags/<snapshot>/<metric>.parquet.
Feed-level metrics (M1/M2) write feed_flags.parquet.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import duckdb
import pyarrow as pa

from .bh import classify_specialty, is_bh_taxonomy

log = logging.getLogger("gnw.flags")

RULES_VERSION = "v0.1"

STATE_FIPS = {
    "AL": "01", "AK": "02", "AZ": "04", "AR": "05", "CA": "06", "CO": "08",
    "CT": "09", "DE": "10", "DC": "11", "FL": "12", "GA": "13", "HI": "15",
    "ID": "16", "IL": "17", "IN": "18", "IA": "19", "KS": "20", "KY": "21",
    "LA": "22", "ME": "23", "MD": "24", "MA": "25", "MI": "26", "MN": "27",
    "MS": "28", "MO": "29", "MT": "30", "NE": "31", "NV": "32", "NH": "33",
    "NJ": "34", "NM": "35", "NY": "36", "NC": "37", "ND": "38", "OH": "39",
    "OK": "40", "OR": "41", "PA": "42", "RI": "44", "SC": "45", "SD": "46",
    "TN": "47", "TX": "48", "UT": "49", "VT": "50", "VA": "51", "WA": "53",
    "WV": "54", "WI": "55", "WY": "56",
}

_EVIDENCE_COLS = (
    "snapshot, metric, subcode, evidence_strength, weight, "
    "source_sha256, record_idx, npi, plan_id, observed, rule_version"
)


def _luhn_npi(npi: str) -> bool:
    if not npi.isdigit() or len(npi) != 10:
        return False
    total, alt = 0, False
    for ch in reversed("80840" + npi):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


class FlagEngine:
    def __init__(self, data_root: Path, snapshot: str, fetch_date: str) -> None:
        self.data_root = data_root
        self.snapshot = snapshot
        self.fetch_date = fetch_date  # YYYY-MM-DD the crawl ran
        self.out = data_root / "flags" / snapshot
        self.out.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect()
        self.con.execute("SET memory_limit='10GB'")
        self.con.execute(f"SET temp_directory='{data_root / 'tmp' / 'duckdb'}'")
        pq = data_root / "parquet" / snapshot
        ref = data_root / "reference" / "parquet"
        views = {
            "providers": pq / "providers" / "*.parquet",
            "addresses": pq / "provider_addresses" / "*.parquet",
            "pplans": pq / "provider_plans" / "*.parquet",
            "nppes": ref / "nppes.parquet",
            "nppes_deact": ref / "nppes_deactivated.parquet",
            "plan_attributes": ref / "plan_attributes.parquet",
            "service_area": ref / "service_area.parquet",
            "plan_county": ref / "plan_county.parquet",
            "zip_county": ref / "zip_county.parquet",
            "adjacency": ref / "county_adjacency.parquet",
        }
        for name, path in views.items():
            self.con.execute(
                f"CREATE VIEW {name} AS SELECT * FROM read_parquet('{path}')"
            )
        self.con.execute(
            "CREATE VIEW manifest AS SELECT * FROM read_json_auto("
            f"'{data_root / 'snapshots' / snapshot / 'manifest.jsonl'}')"
        )
        self.con.execute(
            "CREATE TEMP TABLE state_fips (state VARCHAR, fips2 VARCHAR)"
        )
        self.con.executemany(
            "INSERT INTO state_fips VALUES (?, ?)", list(STATE_FIPS.items())
        )

    def _write(self, metric: str, select_sql: str) -> int:
        dest = self.out / f"{metric}.parquet"
        self.con.execute(
            f"COPY ({select_sql}) TO '{dest}' (FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        n = self.con.execute(f"SELECT count(*) FROM '{dest}'").fetchone()[0]
        log.info("%s: %d evidence rows", metric, n)
        return n

    # -- M3 PLACEHOLDER_VALUE (E1) -------------------------------------------

    def m3_placeholder_value(self) -> int:
        base = f"""
        WITH a AS (
          SELECT source_sha256, record_idx, address, city, state, zip, phone,
                 regexp_replace(coalesce(phone,''), '[^0-9]', '', 'g') AS pd,
                 regexp_replace(coalesce(zip,''), '[^0-9]', '', 'g') AS zd
          FROM addresses
        ),
        phone_f AS (
          SELECT source_sha256, record_idx, 'PHONE' AS subcode, 1.0 AS weight,
                 to_json(struct_pack(example_phone := any_value(phone),
                                     addr_rows := count(*))) AS observed
          FROM a
          WHERE pd = '' OR length(pd) < 10
             OR pd = repeat(substr(pd, 1, 1), length(pd))
          GROUP BY 1, 2
        ),
        zip_f AS (
          SELECT source_sha256, record_idx, 'ZIP' AS subcode, 1.0 AS weight,
                 to_json(struct_pack(example_zip := any_value(zip),
                                     addr_rows := count(*))) AS observed
          FROM a
          WHERE zd IN ('99999','00000') OR length(zd) NOT IN (5, 9)
          GROUP BY 1, 2
        ),
        addr_f AS (
          SELECT source_sha256, record_idx, 'ADDRESS' AS subcode, 1.0 AS weight,
                 to_json(struct_pack(example_address := any_value(address),
                                     addr_rows := count(*))) AS observed
          FROM a
          WHERE coalesce(address,'') = '' OR lower(address) IN ('null','n/a','na','unknown')
          GROUP BY 1, 2
        ),
        date_f AS (
          SELECT source_sha256, record_idx, 'DATE' AS subcode, 0.7 AS weight,
                 to_json(struct_pack(last_updated_on := last_updated_on)) AS observed
          FROM providers
          WHERE last_updated_on IS NOT NULL
            AND (last_updated_on < '2014-01-01' OR last_updated_on > '{self.fetch_date}')
        ),
        allf AS (
          SELECT * FROM phone_f UNION ALL SELECT * FROM zip_f
          UNION ALL SELECT * FROM addr_f UNION ALL SELECT * FROM date_f
        )
        SELECT '{self.snapshot}' AS snapshot, 'M3_PLACEHOLDER_VALUE' AS metric,
               f.subcode, 'E1' AS evidence_strength, f.weight,
               f.source_sha256, f.record_idx, p.npi, NULL AS plan_id,
               f.observed, '{RULES_VERSION}' AS rule_version
        FROM allf f LEFT JOIN providers p USING (source_sha256, record_idx)
        """
        return self._write("M3_PLACEHOLDER_VALUE", base)

    # -- M4 STALE_ATTESTATION (E2) -------------------------------------------

    def m4_stale_attestation(self) -> int:
        sql = f"""
        SELECT '{self.snapshot}' AS snapshot, 'M4_STALE_ATTESTATION' AS metric,
               CASE WHEN last_updated_on < (DATE '{self.fetch_date}' - INTERVAL 365 DAY)::VARCHAR
                    THEN 'STALE_365' ELSE 'STALE_180' END AS subcode,
               'E2' AS evidence_strength,
               CASE WHEN last_updated_on < (DATE '{self.fetch_date}' - INTERVAL 365 DAY)::VARCHAR
                    THEN 0.8 ELSE 0.5 END AS weight,
               source_sha256, record_idx, npi, NULL AS plan_id,
               to_json(struct_pack(last_updated_on := last_updated_on)) AS observed,
               '{RULES_VERSION}' AS rule_version
        FROM providers
        WHERE last_updated_on >= '2014-01-01'
          AND last_updated_on < (DATE '{self.fetch_date}' - INTERVAL 180 DAY)::VARCHAR
        """
        return self._write("M4_STALE_ATTESTATION", sql)

    # -- M5 CALL_CENTER_ONLY (E2 + E3 enhancer) ------------------------------

    def m5_call_center_only(self) -> int:
        attribution = json.loads(
            (Path(__file__).parent / "data" / "phone_attribution.json").read_text()
        )
        rows = [
            {"pd": k, "owner": v["owner"], "owner_class": v["class"]}
            for k, v in attribution.items()
            if not k.startswith("_")
        ]
        self.con.register(
            "phone_attr",
            pa.Table.from_pylist(
                rows,
                schema=pa.schema(
                    [("pd", pa.string()), ("owner", pa.string()), ("owner_class", pa.string())]
                ),
            ),
        )
        sql = f"""
        WITH rec_phone AS (
          SELECT DISTINCT source_sha256, record_idx,
                 regexp_replace(coalesce(phone,''), '[^0-9]', '', 'g') AS pd
          FROM addresses
          WHERE length(regexp_replace(coalesce(phone,''), '[^0-9]', '', 'g')) >= 10
        ),
        file_n AS (
          SELECT source_sha256, count(*) AS n
          FROM (SELECT DISTINCT source_sha256, record_idx FROM rec_phone) GROUP BY 1
        ),
        conc AS (
          SELECT r.source_sha256, r.pd, count(*) AS on_records
          FROM rec_phone r JOIN file_n USING (source_sha256)
          GROUP BY r.source_sha256, r.pd, file_n.n
          HAVING count(*) >= greatest(50, 0.01 * any_value(file_n.n))
        ),
        flagged AS (
          SELECT r.source_sha256, r.record_idx,
                 count(*) AS n_phones,
                 count(c.pd) AS n_conc,
                 any_value(c.pd) AS conc_phone,
                 max(c.on_records) AS conc_on_records
          FROM rec_phone r
          LEFT JOIN conc c USING (source_sha256, pd)
          GROUP BY 1, 2
          HAVING count(*) = count(c.pd)
        ),
        nppes_phone AS (
          SELECT npi, regexp_replace(coalesce(practice_phone,''), '[^0-9]', '', 'g') AS npd
          FROM nppes
        )
        SELECT '{self.snapshot}' AS snapshot, 'M5_CALL_CENTER_ONLY' AS metric,
               'CALL_CENTER_ONLY' AS subcode, 'E2' AS evidence_strength, 0.6 AS weight,
               f.source_sha256, f.record_idx, p.npi, NULL AS plan_id,
               to_json(struct_pack(
                 concentration_phone := f.conc_phone,
                 shared_with_records := f.conc_on_records,
                 owner := coalesce(pa2.owner, 'unattributed'),
                 owner_class := coalesce(pa2.owner_class, 'unattributed'),
                 nppes_divergent_phone := (np.npd IS NOT NULL AND length(np.npd) >= 10
                                           AND np.npd <> f.conc_phone)
               )) AS observed,
               '{RULES_VERSION}' AS rule_version
        FROM flagged f
        LEFT JOIN providers p USING (source_sha256, record_idx)
        LEFT JOIN phone_attr pa2 ON pa2.pd = f.conc_phone
        LEFT JOIN nppes_phone np ON np.npi = p.npi
        """
        return self._write("M5_CALL_CENTER_ONLY", sql)

    # -- M6 ADDRESS_INFLATION (E2) -------------------------------------------

    def m6_address_inflation(self) -> int:
        sql = f"""
        WITH uniq AS (
          SELECT source_sha256, record_idx,
                 count(DISTINCT coalesce(address,'') || '|' || coalesce(city,'') || '|' ||
                       coalesce(state,'')) AS n_addr,
                 count(DISTINCT state) AS n_states
          FROM addresses GROUP BY 1, 2
        ),
        joined AS (
          SELECT u.*, p.npi, p.type, p.specialties
          FROM uniq u JOIN providers p USING (source_sha256, record_idx)
          WHERE (p.type = 'INDIVIDUAL' AND u.n_addr > 10
                 AND NOT regexp_matches(lower(coalesce(p.specialties,'')),
                     'radiolog|patholog|anesthesiolog|emergency medicine|hospitalist'))
             OR (coalesce(p.type,'') <> 'INDIVIDUAL' AND u.n_addr > 25)
        )
        SELECT '{self.snapshot}' AS snapshot, 'M6_ADDRESS_INFLATION' AS metric,
               CASE WHEN type = 'INDIVIDUAL' AND n_addr > 25 THEN 'INDIVIDUAL_GT25'
                    WHEN type = 'INDIVIDUAL' THEN 'INDIVIDUAL_GT10'
                    WHEN n_addr > 100 THEN 'ORG_GT100' ELSE 'ORG_GT25' END AS subcode,
               'E2' AS evidence_strength,
               CASE WHEN (type = 'INDIVIDUAL' AND n_addr > 25) OR n_addr > 100
                    THEN 0.6 ELSE 0.4 END AS weight,
               source_sha256, record_idx, npi, NULL AS plan_id,
               to_json(struct_pack(unique_addresses := n_addr, states := n_states,
                                   type := type)) AS observed,
               '{RULES_VERSION}' AS rule_version
        FROM joined
        """
        return self._write("M6_ADDRESS_INFLATION", sql)

    # -- M7 OUT_OF_AREA_LISTING (E2) -----------------------------------------

    def m7_out_of_area(self) -> int:
        sql = f"""
        WITH attach AS (
          SELECT DISTINCT source_sha256, record_idx, substr(plan_id, 1, 14) AS scid
          FROM pplans
          WHERE upper(coalesce(plan_id_type,'')) LIKE 'HIOS%'
            AND length(plan_id) >= 14
            AND regexp_matches(substr(plan_id, 1, 14), '^\\d{{5}}[A-Z]{{2}}\\d{{7}}$')
            AND (years IS NULL OR years LIKE '%2026%')
        ),
        scid_sa AS (
          SELECT DISTINCT standardcomponentid AS scid, serviceareaid, statecode
          FROM plan_attributes
        ),
        sa_counties AS (
          SELECT sa.serviceareaid, sa.statecode,
                 CASE WHEN upper(coalesce(sa.coverentirestate,'')) = 'YES' THEN NULL
                      ELSE lpad(regexp_replace(sa.county, '[^0-9]', '', 'g'), 5, '0') END AS county
          FROM service_area sa
        ),
        state_counties AS (
          SELECT sf.state, a.county_fips
          FROM state_fips sf
          JOIN (SELECT DISTINCT county_fips FROM adjacency) a
            ON substr(a.county_fips, 1, 2) = sf.fips2
        ),
        allowed AS (
          SELECT s.scid, coalesce(sc.county, stc.county_fips) AS county
          FROM scid_sa s
          JOIN sa_counties sc USING (serviceareaid)
          LEFT JOIN state_counties stc
            ON sc.county IS NULL AND stc.state = s.statecode
          WHERE coalesce(sc.county, stc.county_fips) IS NOT NULL
        ),
        allowed_adj AS (
          SELECT scid, county FROM allowed
          UNION
          SELECT al.scid, ad.adjacent_fips AS county
          FROM allowed al JOIN adjacency ad ON ad.county_fips = al.county
        ),
        rec_county AS (
          SELECT DISTINCT a.source_sha256, a.record_idx, z.county_fips
          FROM addresses a
          JOIN zip_county z
            ON z.zip = substr(regexp_replace(coalesce(a.zip,''), '[^0-9]', '', 'g'), 1, 5)
        ),
        mappable AS (
          SELECT DISTINCT source_sha256, record_idx FROM rec_county
        ),
        in_area AS (
          SELECT DISTINCT t.source_sha256, t.record_idx, t.scid
          FROM attach t
          JOIN rec_county rc USING (source_sha256, record_idx)
          JOIN allowed_adj aa ON aa.scid = t.scid AND aa.county = rc.county_fips
        ),
        flagged AS (
          SELECT t.* FROM attach t
          JOIN mappable m USING (source_sha256, record_idx)
          JOIN (SELECT DISTINCT scid FROM allowed) k ON k.scid = t.scid
          ANTI JOIN in_area i
            ON i.source_sha256 = t.source_sha256 AND i.record_idx = t.record_idx
           AND i.scid = t.scid
        )
        SELECT '{self.snapshot}' AS snapshot, 'M7_OUT_OF_AREA_LISTING' AS metric,
               'OUT_OF_AREA' AS subcode, 'E2' AS evidence_strength, 0.8 AS weight,
               f.source_sha256, f.record_idx, p.npi, f.scid AS plan_id,
               to_json(struct_pack(
                 record_states := (SELECT string_agg(DISTINCT a2.state, '|')
                                   FROM addresses a2
                                   WHERE a2.source_sha256 = f.source_sha256
                                     AND a2.record_idx = f.record_idx),
                 plan_state := substr(f.scid, 6, 2)
               )) AS observed,
               '{RULES_VERSION}' AS rule_version
        FROM flagged f
        LEFT JOIN providers p USING (source_sha256, record_idx)
        """
        return self._write("M7_OUT_OF_AREA_LISTING", sql)

    # -- M8 ACCEPTING_UNKNOWN (E1) -------------------------------------------

    def m8_accepting_unknown(self) -> int:
        sql = f"""
        SELECT '{self.snapshot}' AS snapshot, 'M8_ACCEPTING_UNKNOWN' AS metric,
               'ACCEPTING_UNKNOWN' AS subcode, 'E1' AS evidence_strength, 0.3 AS weight,
               source_sha256, record_idx, npi, NULL AS plan_id,
               to_json(struct_pack(accepting := accepting)) AS observed,
               '{RULES_VERSION}' AS rule_version
        FROM providers
        WHERE type = 'INDIVIDUAL'
          AND (accepting IS NULL OR accepting = '' OR lower(accepting) = 'unknown')
        """
        return self._write("M8_ACCEPTING_UNKNOWN", sql)

    # -- M9 NPI_REGISTRY_STATUS (E1 / E3) ------------------------------------

    def m9_npi_registry_status(self) -> int:
        npis = [
            r[0]
            for r in self.con.execute(
                "SELECT DISTINCT npi FROM providers "
                "WHERE npi IS NOT NULL AND regexp_matches(npi, '^\\d{10}$')"
            ).fetchall()
        ]
        bad = [n for n in npis if not _luhn_npi(n)]
        self.con.register(
            "luhn_bad",
            pa.Table.from_pylist([{"npi": n} for n in bad], schema=pa.schema([("npi", pa.string())])),
        )
        log.info("M9: %d distinct well-formed NPIs, %d Luhn-invalid", len(npis), len(bad))
        sql = f"""
        WITH missing AS (
          SELECT source_sha256, record_idx, npi, 'MISSING_OR_MALFORMED' AS subcode,
                 'E1' AS strength, 1.0 AS weight,
                 to_json(struct_pack(npi := npi)) AS observed
          FROM providers
          WHERE npi IS NULL OR NOT regexp_matches(npi, '^\\d{{10}}$')
        ),
        luhn AS (
          SELECT p.source_sha256, p.record_idx, p.npi, 'LUHN_INVALID' AS subcode,
                 'E1' AS strength, 1.0 AS weight,
                 to_json(struct_pack(npi := p.npi)) AS observed
          FROM providers p JOIN luhn_bad USING (npi)
        ),
        deact AS (
          SELECT p.source_sha256, p.record_idx, p.npi, 'DEACTIVATED' AS subcode,
                 'E3' AS strength, 0.7 AS weight,
                 to_json(struct_pack(npi := p.npi,
                                     deactivation_date := d.deactivation_date)) AS observed
          FROM providers p
          JOIN nppes_deact d USING (npi)
          ANTI JOIN nppes n ON n.npi = p.npi
        ),
        allf AS (
          SELECT * FROM missing UNION ALL SELECT * FROM luhn UNION ALL SELECT * FROM deact
        )
        SELECT '{self.snapshot}' AS snapshot, 'M9_NPI_REGISTRY_STATUS' AS metric,
               subcode, strength AS evidence_strength, weight,
               source_sha256, record_idx, npi, NULL AS plan_id, observed,
               '{RULES_VERSION}' AS rule_version
        FROM allf
        """
        return self._write("M9_NPI_REGISTRY_STATUS", sql)

    # -- M10 TAXONOMY_MISMATCH (E3, BH-protective scope in v0) ----------------

    def m10_taxonomy_mismatch(self) -> int:
        distinct = [
            r[0]
            for r in self.con.execute(
                "SELECT DISTINCT specialties FROM providers WHERE specialties IS NOT NULL"
            ).fetchall()
        ]
        verdicts = [
            {"specialties": s, "verdict": classify_specialty(s)} for s in distinct
        ]
        self.con.register(
            "spec_verdict",
            pa.Table.from_pylist(
                verdicts,
                schema=pa.schema([("specialties", pa.string()), ("verdict", pa.string())]),
            ),
        )
        bh_codes = [
            r[0]
            for r in self.con.execute("SELECT code FROM read_parquet('"
                + str(self.data_root / "reference" / "parquet" / "nucc_taxonomy.parquet")
                + "')").fetchall()
            if is_bh_taxonomy(r[0])
        ]
        self.con.register(
            "bh_codes",
            pa.Table.from_pylist([{"code": c} for c in bh_codes], schema=pa.schema([("code", pa.string())])),
        )
        log.info("M10: %d distinct specialty strings, %d BH NUCC codes", len(distinct), len(bh_codes))
        sql = f"""
        WITH nppes_bh AS (
          SELECT DISTINCT n.npi
          FROM (SELECT npi, unnest(string_split(taxonomies, '|')) AS code
                FROM nppes WHERE taxonomies IS NOT NULL) n
          JOIN bh_codes b ON b.code = n.code
        )
        SELECT '{self.snapshot}' AS snapshot, 'M10_TAXONOMY_MISMATCH' AS metric,
               'BH_STRING_NOT_BH_TAXONOMY' AS subcode, 'E3' AS evidence_strength,
               0.5 AS weight,
               p.source_sha256, p.record_idx, p.npi, NULL AS plan_id,
               to_json(struct_pack(specialties := p.specialties,
                                   nppes_taxonomies := n.taxonomies)) AS observed,
               '{RULES_VERSION}' AS rule_version
        FROM providers p
        JOIN spec_verdict v ON v.specialties = p.specialties AND v.verdict = 'bh'
        JOIN nppes n ON n.npi = p.npi AND n.taxonomies IS NOT NULL
        ANTI JOIN nppes_bh b ON b.npi = p.npi
        """
        return self._write("M10_TAXONOMY_MISMATCH", sql)

    # -- Feed-level M1 / M2 ---------------------------------------------------

    def feed_flags(self) -> int:
        restricted = json.loads(
            (Path(__file__).parent / "data" / "access_restricted.json").read_text()
        )
        sql = f"""
        WITH ok_urls AS (
          SELECT DISTINCT url FROM manifest WHERE sha256 IS NOT NULL
        ),
        idx AS (
          SELECT DISTINCT url, issuer_ids, states FROM manifest WHERE role = 'index'
        ),
        dead_idx AS (
          SELECT i.url, i.issuer_ids, i.states FROM idx i
          ANTI JOIN ok_urls o ON o.url = i.url
        ),
        file_share AS (
          SELECT index_url,
                 count(DISTINCT url) AS total,
                 count(DISTINCT CASE WHEN url IN (SELECT url FROM ok_urls) THEN url END) AS ok
          FROM manifest WHERE role IN ('provider','plan') AND index_url IS NOT NULL
          GROUP BY 1
        ),
        dead_files AS (
          SELECT i.url, i.issuer_ids, i.states FROM idx i
          JOIN file_share fs ON fs.index_url = i.url
          WHERE fs.ok < 0.5 * fs.total
        ),
        m1 AS (
          SELECT 'M1_DEAD_FEED' AS metric, 'INDEX_UNREACHABLE' AS subcode, url,
                 issuer_ids, states FROM dead_idx
          UNION ALL
          SELECT 'M1_DEAD_FEED', 'MAJORITY_FILES_FAILED', url, issuer_ids, states
          FROM dead_files
        ),
        m2 AS (
          SELECT 'M2_ACCESS_RESTRICTED' AS metric, 'BROWSER_UA_ONLY' AS subcode,
                 i.url, i.issuer_ids, i.states
          FROM idx i
          WHERE {" OR ".join(f"i.url LIKE '%{h}%'" for h in restricted["hosts"])}
        )
        SELECT '{self.snapshot}' AS snapshot, metric, subcode, 'E1' AS evidence_strength,
               url, issuer_ids, states, '{RULES_VERSION}' AS rule_version
        FROM (SELECT * FROM m1 UNION ALL SELECT * FROM m2)
        """
        dest = self.out / "feed_flags.parquet"
        self.con.execute(f"COPY ({sql}) TO '{dest}' (FORMAT PARQUET, COMPRESSION ZSTD)")
        n = self.con.execute(f"SELECT count(*) FROM '{dest}'").fetchone()[0]
        log.info("feed_flags: %d rows", n)
        return n

    METRICS = {
        "m3": m3_placeholder_value,
        "m4": m4_stale_attestation,
        "m5": m5_call_center_only,
        "m6": m6_address_inflation,
        "m7": m7_out_of_area,
        "m8": m8_accepting_unknown,
        "m9": m9_npi_registry_status,
        "m10": m10_taxonomy_mismatch,
        "feed": feed_flags,
    }

    def run(self, only: str | None = None) -> dict[str, int]:
        results = {}
        for name, fn in self.METRICS.items():
            if only and name != only:
                continue
            results[name] = fn(self)
        return results
