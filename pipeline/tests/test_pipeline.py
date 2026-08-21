import gzip
import json
import hashlib
from pathlib import Path

from gnw.quirks import quirks_for, BROWSER_UA
from gnw.seed import load_seeds, medical_seeds
from gnw.store import EvidenceStore, ManifestRow


def test_quirks_defaults_and_overrides():
    default = quirks_for("https://example.com/x.json")
    assert default.user_agent == BROWSER_UA
    assert default.accept == "*/*"
    elevance = quirks_for("https://www22.elevancehealth.com/idx.json")
    assert elevance.max_retries == 5


def test_seed_scope_matches_scoping_findings():
    seeds = load_seeds()
    assert len(seeds) == 108
    medical = medical_seeds()
    rows = sum(len(s.issuer_ids) for s in medical)
    # Scoping found 186 medical issuer rows of 346.
    assert rows == 186
    # Known dental host must be out of scope; known medical hosts in scope.
    hosts = {s.url for s in medical}
    assert not any("cms.deltadental.com" in u for u in hosts)
    assert any("api.centene.com" in u for u in hosts)
    assert any("chppayment.christushealth.org" in u for u in hosts)
    # Dead-but-mandated index stays in scope: failures are findings.
    assert any("bcbsnc.com" in u for u in hosts)


def test_store_blob_roundtrip_and_dedup(tmp_path: Path):
    store = EvidenceStore(tmp_path / "data")
    payload = json.dumps({"provider_urls": ["https://x/p1.json"]}).encode()
    sha = hashlib.sha256(payload).hexdigest()

    src = tmp_path / "content"
    src.write_bytes(payload)
    store.add_blob(sha, src)
    assert store.has_blob(sha)
    assert not src.exists()  # consumed
    assert store.read_json(sha)["provider_urls"] == ["https://x/p1.json"]

    # Dedup: second add consumes the file but writes nothing new.
    src2 = tmp_path / "content2"
    src2.write_bytes(payload)
    before = store.blob_path(sha).stat().st_mtime_ns
    store.add_blob(sha, src2)
    assert store.blob_path(sha).stat().st_mtime_ns == before

    # Blob is stored gzip-compressed on disk.
    with gzip.open(store.blob_path(sha), "rb") as fh:
        assert fh.read() == payload


def test_manifest_append_and_resume(tmp_path: Path):
    store = EvidenceStore(tmp_path / "data")
    row = ManifestRow(
        snapshot="2026-08",
        role="index",
        url="https://x/idx.json",
        index_url=None,
        issuer_ids=["11111"],
        states=["TX"],
        fetched_at="2026-08-21T00:00:00Z",
        status=200,
        sha256="a" * 64,
    )
    store.append(row)
    fail = ManifestRow(
        snapshot="2026-08",
        role="provider",
        url="https://x/p.json",
        index_url="https://x/idx.json",
        issuer_ids=["11111"],
        states=["TX"],
        fetched_at="2026-08-21T00:00:01Z",
        status=404,
        error="HTTP 404",
    )
    store.append(fail)
    rows = store.load_manifest("2026-08")
    assert len(rows) == 2
    assert store.fetched_ok_urls("2026-08") == {"https://x/idx.json"}


def _put_blob(store: EvidenceStore, obj) -> str:
    payload = json.dumps(obj).encode()
    sha = hashlib.sha256(payload).hexdigest()
    src = store.root / f"tmp-{sha[:8]}"
    src.write_bytes(payload)
    store.add_blob(sha, src)
    return sha


def test_parse_provider_and_plan_blobs(tmp_path: Path):
    import pyarrow.parquet as pq
    from gnw.parse import parse_snapshot

    store = EvidenceStore(tmp_path / "data")
    provider_records = [
        {
            "npi": "1255010732",
            "type": "INDIVIDUAL",
            "name": {"first": "HADA", "last": "TILLERO"},
            "gender": "Female",
            "accepting": "accepting",
            "last_updated_on": "2026-08-01",
            "specialty": ["Psychiatry", "Addiction Medicine"],
            "languages": ["English", "Spanish"],
            "addresses": [
                {"address": "1 Main St", "city": "Muncie", "state": "IN",
                 "zip": "47304", "phone": "7652133939"}
            ],
            "plans": [
                {"plan_id_type": "HIOS-PLAN-ID", "plan_id": "28856IN0220001",
                 "network_tier": "PPO", "years": [2026]}
            ],
        },
        {
            "npi": "1932171634",
            "type": "FACILITY",
            "facility_name": "Clinic X",
            "facility_type": ["Hospital"],
            # no addresses/plans keys at all — must not crash
        },
    ]
    sha_p = _put_blob(store, provider_records)
    # dict-wrapped variant must also parse
    sha_w = _put_blob(store, {"providers": provider_records[:1]})
    sha_plan = _put_blob(
        store,
        [{"plan_id_type": "HIOS-PLAN-ID", "plan_id": "28856IN0220001",
          "marketing_name": "Test Plan", "network": [{"network_tier": "PPO"}],
          "last_updated_on": "2026-08-01"}],
    )
    for sha, role in [(sha_p, "provider"), (sha_w, "provider"), (sha_plan, "plan")]:
        store.append(ManifestRow(
            snapshot="t", role=role, url=f"https://x/{sha[:6]}.json", index_url="https://x/i.json",
            issuer_ids=["28856"], states=["IN"], fetched_at="2026-08-21T00:00:00Z",
            status=200, sha256=sha,
        ))

    stats = parse_snapshot(store, "t", tmp_path / "pq")
    assert stats.failed == 0
    assert stats.provider_records == 3  # 2 + 1 wrapped
    assert stats.plan_records == 1

    prov = pq.read_table(tmp_path / "pq" / "t" / "providers").to_pylist()
    assert len(prov) == 3
    ind = next(r for r in prov if r["type"] == "INDIVIDUAL" and r["source_sha256"] == sha_p)
    assert ind["specialties"] == "Psychiatry|Addiction Medicine"
    assert ind["specialty_count"] == 2
    assert ind["gender"] == "Female"
    fac = next(r for r in prov if r["type"] == "FACILITY")
    assert fac["facility_name"] == "Clinic X"
    assert fac["addresses_count"] == 0

    addrs = pq.read_table(tmp_path / "pq" / "t" / "provider_addresses").to_pylist()
    assert {a["zip"] for a in addrs} == {"47304"}
    plans = pq.read_table(tmp_path / "pq" / "t" / "provider_plans").to_pylist()
    assert plans[0]["plan_id"] == "28856IN0220001" and plans[0]["years"] == "2026"

    # resumability: second run parses nothing new
    stats2 = parse_snapshot(store, "t", tmp_path / "pq")
    assert stats2.blobs == 0 and stats2.skipped == 3


def test_bh_classifier():
    from gnw.bh import classify_specialty, classify_record, is_bh_taxonomy

    assert classify_specialty("Psychiatry") == "bh"
    assert classify_specialty("PSYCHIATRY") == "bh"
    assert classify_specialty("Physical Therapy") == "not_bh"
    assert classify_specialty("Psychologist|Psychology") == "bh"  # multi-value split
    assert classify_specialty("Psychiatry &amp; Neurology, Psychiatry") in ("bh", "ambiguous")
    assert classify_specialty(None) == "unknown"
    # unseen string with BH-ish token must be ambiguous, never auto-included
    assert classify_specialty("Zebra Mental Wellness Consultant") == "ambiguous"
    # developMENTAL false positive guard
    assert classify_specialty("Developmental Disabilities Aide") != "bh"

    assert is_bh_taxonomy("2084P0800X")       # psychiatry
    assert not is_bh_taxonomy("2084N0400X")   # neurology, 2084-family exclude
    assert is_bh_taxonomy("101YM0800X")       # mental health counselor
    assert not is_bh_taxonomy("225100000X")   # physical therapist

    assert classify_record("Psychiatry", "2084P0800X") == "bh_taxonomy"
    assert classify_record("Psychiatry", "225100000X") == "bh_string"
    assert classify_record("Physical Therapy", None) == "not_bh"
