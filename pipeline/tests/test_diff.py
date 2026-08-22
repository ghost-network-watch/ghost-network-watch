import json
from pathlib import Path

import duckdb

from gnw.diff import KEYED_METRICS, build_diff, previous_snapshot

CON = duckdb.connect()


def _write(path: Path, rows: list[dict], columns: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    cols = ", ".join(f'"{c}" {t}' for c, t in columns.items())
    CON.execute(f"CREATE OR REPLACE TEMP TABLE t ({cols})")
    for r in rows:
        CON.execute(
            f"INSERT INTO t VALUES ({', '.join('?' for _ in columns)})",
            [r.get(c) for c in columns],
        )
    CON.execute(f"COPY t TO '{path}' (FORMAT PARQUET)")


FLAG_COLS = {"metric": "VARCHAR", "subcode": "VARCHAR", "npi": "VARCHAR", "source_sha256": "VARCHAR"}
M7_COLS = {"plan_id": "VARCHAR"}
SCORE_COLS = {
    "scid": "VARCHAR", "county_fips": "VARCHAR", "scope": "VARCHAR",
    "grade": "VARCHAR", "score": "DOUBLE", "thin_roster": "BOOLEAN",
}


def _snapshot(root: Path, snap: str, m3_rows, m7_rows, score_rows, sha_issuers) -> None:
    flags = root / "flags" / snap
    for m in KEYED_METRICS:
        _write(flags / f"{m}.parquet", m3_rows if m == "M3_PLACEHOLDER_VALUE" else [], FLAG_COLS)
    _write(flags / "M7_OUT_OF_AREA_LISTING.parquet", m7_rows, M7_COLS)
    _write(
        root / "scores" / snap / "plan_county_scores.parquet", score_rows, SCORE_COLS
    )
    mdir = root / "snapshots" / snap
    mdir.mkdir(parents=True, exist_ok=True)
    with open(mdir / "manifest.jsonl", "w") as fh:
        for sha, issuers in sha_issuers.items():
            fh.write(json.dumps({"sha256": sha, "role": "provider", "issuer_ids": issuers}) + "\n")


def test_diff_statuses_and_grades(tmp_path: Path):
    root = tmp_path
    # January: NPIs 1 and 2 flagged for issuer 11111; plan graded F; 5 out-of-area rows.
    _snapshot(
        root, "2025-01",
        m3_rows=[
            {"metric": "M3_PLACEHOLDER_VALUE", "subcode": "PHONE", "npi": "1000000001", "source_sha256": "aa"},
            {"metric": "M3_PLACEHOLDER_VALUE", "subcode": "PHONE", "npi": "1000000002", "source_sha256": "aa"},
            {"metric": "M3_PLACEHOLDER_VALUE", "subcode": "ZIP", "npi": None, "source_sha256": "aa"},
        ],
        m7_rows=[{"plan_id": "11111TX0000001"}] * 5,
        score_rows=[
            {"scid": "11111TX0000001", "county_fips": "48001", "scope": "bh",
             "grade": "F", "score": 40.0, "thin_roster": False},
        ],
        sha_issuers={"aa": ["11111"]},
    )
    # February: file regenerated (new sha). NPI 1 persists, NPI 2 fixed,
    # NPI 3 newly broken. Null-NPI count drops to 0, M7 count rises to 8.
    # The plan's grade improves to C.
    _snapshot(
        root, "2025-02",
        m3_rows=[
            {"metric": "M3_PLACEHOLDER_VALUE", "subcode": "PHONE", "npi": "1000000001", "source_sha256": "bb"},
            {"metric": "M3_PLACEHOLDER_VALUE", "subcode": "PHONE", "npi": "1000000003", "source_sha256": "bb"},
        ],
        m7_rows=[{"plan_id": "11111TX0000001"}] * 8,
        score_rows=[
            {"scid": "11111TX0000001", "county_fips": "48001", "scope": "bh",
             "grade": "C", "score": 72.0, "thin_roster": False},
        ],
        sha_issuers={"bb": ["11111"]},
    )

    assert previous_snapshot(root, "2025-02") == "2025-01"
    summary = build_diff(root, "2025-02")
    assert summary["resolved"] == 1 and summary["new"] == 1 and summary["persisting"] == 1
    assert summary["grades_improved"] == 1 and summary["grades_declined"] == 0

    status = {
        r[0]: r[1]
        for r in CON.execute(
            f"SELECT npi, status FROM read_parquet('{root / 'diff' / '2025-02' / 'flag_status.parquet'}')"
        ).fetchall()
    }
    assert status == {"1000000001": "persisting", "1000000002": "resolved", "1000000003": "new"}

    deltas = {
        r[0]: r[1]
        for r in CON.execute(
            f"SELECT metric, delta FROM read_parquet('{root / 'diff' / '2025-02' / 'count_changes.parquet'}')"
        ).fetchall()
    }
    assert deltas["M7_OUT_OF_AREA_LISTING"] == 3
    assert deltas["M3_PLACEHOLDER_VALUE"] == -1  # the null-NPI ZIP row went away

    resolved = CON.execute(
        f"SELECT npi, resolved_in FROM read_csv_auto('{root / 'diff' / '2025-02' / 'resolved_flags.csv.gz'}', all_varchar=true)"
    ).fetchall()
    assert resolved == [("1000000002", "2025-02")]


def test_first_snapshot_marker(tmp_path: Path):
    _snapshot(
        tmp_path, "2025-01", m3_rows=[], m7_rows=[],
        score_rows=[{"scid": "11111TX0000001", "county_fips": "48001", "scope": "bh",
                     "grade": "A", "score": 95.0, "thin_roster": False}],
        sha_issuers={"aa": ["11111"]},
    )
    summary = build_diff(tmp_path, "2025-01")
    assert summary["first_snapshot"] is True
