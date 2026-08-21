"""Polite streaming fetcher.

One request at a time per host (politeness), browser UA (mandated files that
403 bots), streaming download with size cap, transparent handling of files
whose *payload* is gzip (magic bytes) as opposed to transfer-encoding gzip
(requests already decodes that). Content is hashed over the decompressed
bytes so the hash is stable regardless of how the server chose to serve it.
"""

from __future__ import annotations

import hashlib
import tempfile
import threading
import time
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import requests

from .quirks import quirks_for

_CHUNK = 1 << 16


@dataclass
class FetchResult:
    url: str
    status: int | None = None
    final_url: str | None = None
    sha256: str | None = None
    bytes_content: int = 0
    content_type: str | None = None
    last_modified: str | None = None
    etag: str | None = None
    was_gzip_payload: bool = False
    elapsed_s: float = 0.0
    error: str | None = None
    tmp_path: Path | None = field(default=None, repr=False)

    @property
    def ok(self) -> bool:
        return self.error is None and self.status == 200


class _HostGate:
    """Serializes requests per host and enforces a minimum interval."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._hosts: dict[str, threading.Lock] = {}
        self._last: dict[str, float] = {}

    def acquire(self, host: str, min_interval: float) -> None:
        with self._lock:
            gate = self._hosts.setdefault(host, threading.Lock())
        gate.acquire()
        wait = self._last.get(host, 0.0) + min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)

    def release(self, host: str) -> None:
        self._last[host] = time.monotonic()
        self._hosts[host].release()


class PoliteFetcher:
    def __init__(self, tmp_dir: Path) -> None:
        self.tmp_dir = tmp_dir
        self.tmp_dir.mkdir(parents=True, exist_ok=True)
        self._gate = _HostGate()
        self._local = threading.local()

    def _session(self) -> requests.Session:
        if not hasattr(self._local, "session"):
            self._local.session = requests.Session()
        return self._local.session

    def fetch(self, url: str) -> FetchResult:
        q = quirks_for(url)
        host = urlparse(url).netloc.lower()
        result = FetchResult(url=url)
        started = time.monotonic()
        self._gate.acquire(host, q.rate_limit_seconds)
        try:
            for attempt in range(q.max_retries):
                result = self._fetch_once(url, q)
                retryable = result.error is not None or (
                    result.status is not None and result.status >= 500
                )
                if not retryable:
                    break
                if attempt < q.max_retries - 1:
                    time.sleep(q.backoff_base_seconds * (2**attempt))
        finally:
            self._gate.release(host)
        result.elapsed_s = round(time.monotonic() - started, 3)
        return result

    def _fetch_once(self, url: str, q) -> FetchResult:
        result = FetchResult(url=url)
        try:
            resp = self._session().get(
                url,
                headers={"User-Agent": q.user_agent, "Accept": q.accept},
                stream=True,
                timeout=(q.connect_timeout, q.read_timeout),
                allow_redirects=True,
            )
        except requests.RequestException as exc:
            result.error = f"{type(exc).__name__}: {exc}"[:300]
            return result

        with resp:
            result.status = resp.status_code
            result.final_url = resp.url
            result.content_type = resp.headers.get("Content-Type")
            result.last_modified = resp.headers.get("Last-Modified")
            result.etag = resp.headers.get("ETag")
            if resp.status_code != 200:
                return result

            hasher = hashlib.sha256()
            gunzip = None
            total = 0
            tmp = tempfile.NamedTemporaryFile(
                dir=self.tmp_dir, prefix="fetch-", delete=False
            )
            tmp_path = Path(tmp.name)
            try:
                with tmp:
                    first = True
                    for chunk in resp.iter_content(_CHUNK):
                        if first:
                            first = False
                            if chunk[:2] == b"\x1f\x8b":
                                result.was_gzip_payload = True
                                gunzip = zlib.decompressobj(47)
                        if gunzip is not None:
                            chunk = gunzip.decompress(chunk)
                        total += len(chunk)
                        if total > q.max_bytes:
                            raise _SizeCap()
                        hasher.update(chunk)
                        tmp.write(chunk)
                    if gunzip is not None:
                        tail = gunzip.flush()
                        total += len(tail)
                        hasher.update(tail)
                        tmp.write(tail)
            except _SizeCap:
                tmp_path.unlink(missing_ok=True)
                result.error = f"size_cap_exceeded:{q.max_bytes}"
                return result
            except (requests.RequestException, zlib.error, OSError) as exc:
                tmp_path.unlink(missing_ok=True)
                result.error = f"{type(exc).__name__}: {exc}"[:300]
                return result

            result.sha256 = hasher.hexdigest()
            result.bytes_content = total
            result.tmp_path = tmp_path
            return result


class _SizeCap(Exception):
    pass
