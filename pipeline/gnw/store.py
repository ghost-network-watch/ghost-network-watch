"""Content-addressed evidence store + per-snapshot manifest.

Blobs are keyed by sha256 of the *decompressed* content and stored
gzip-compressed; identical files fetched in different snapshots dedupe to one
blob. The manifest is the evidence backbone: one JSONL row per fetch attempt
(including failures — a dead feed is product data, not an error to hide).
"""

from __future__ import annotations

import gzip
import json
import shutil
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class ManifestRow:
    snapshot: str
    role: str  # index | provider | plan
    url: str
    index_url: str | None
    issuer_ids: list[str]
    states: list[str]
    fetched_at: str
    status: int | None = None
    sha256: str | None = None
    bytes_content: int = 0
    content_type: str | None = None
    last_modified: str | None = None
    etag: str | None = None
    final_url: str | None = None
    was_gzip_payload: bool = False
    elapsed_s: float = 0.0
    error: str | None = None
    extra: dict = field(default_factory=dict)


class EvidenceStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.blobs = root / "blobs"
        self.snapshots = root / "snapshots"
        self.blobs.mkdir(parents=True, exist_ok=True)
        self.snapshots.mkdir(parents=True, exist_ok=True)
        self._write_lock = threading.Lock()

    # -- blobs ---------------------------------------------------------------

    def blob_path(self, sha256: str) -> Path:
        return self.blobs / sha256[:2] / f"{sha256}.gz"

    def has_blob(self, sha256: str) -> bool:
        return self.blob_path(sha256).exists()

    def add_blob(self, sha256: str, content_path: Path) -> int:
        """Compress content_path into the store; returns stored bytes."""
        dest = self.blob_path(sha256)
        if dest.exists():
            content_path.unlink(missing_ok=True)
            return dest.stat().st_size
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".gz.tmp")
        with open(content_path, "rb") as src, gzip.open(tmp, "wb", compresslevel=6) as out:
            shutil.copyfileobj(src, out, 1 << 20)
        tmp.rename(dest)
        content_path.unlink(missing_ok=True)
        return dest.stat().st_size

    def open_blob(self, sha256: str):
        return gzip.open(self.blob_path(sha256), "rb")

    def read_json(self, sha256: str):
        with self.open_blob(sha256) as fh:
            return json.load(fh)

    # -- manifest ------------------------------------------------------------

    def manifest_path(self, snapshot: str) -> Path:
        d = self.snapshots / snapshot
        d.mkdir(parents=True, exist_ok=True)
        return d / "manifest.jsonl"

    def append(self, row: ManifestRow) -> None:
        line = json.dumps(asdict(row), separators=(",", ":"))
        with self._write_lock:
            with open(self.manifest_path(row.snapshot), "a") as fh:
                fh.write(line + "\n")

    def load_manifest(self, snapshot: str) -> list[dict]:
        path = self.manifest_path(snapshot)
        if not path.exists():
            return []
        with open(path) as fh:
            return [json.loads(l) for l in fh if l.strip()]

    def fetched_ok_urls(self, snapshot: str) -> set[str]:
        return {
            r["url"]
            for r in self.load_manifest(snapshot)
            if r.get("sha256") and r.get("status") == 200
        }
