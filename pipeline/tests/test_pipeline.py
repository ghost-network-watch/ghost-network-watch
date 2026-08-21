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
