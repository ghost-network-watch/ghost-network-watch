"""Crawl orchestration: indexes first, then the provider/plan files they declare.

Provider URLs are re-resolved from each index on every run (CareSource et al.
use date-stamped paths that rot). Failures are recorded in the manifest, not
retried away — a 404 on a mandated file is a finding.
"""

from __future__ import annotations

import json
import logging
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .fetch import FetchResult, PoliteFetcher
from .seed import IndexSeed
from .store import EvidenceStore, ManifestRow

log = logging.getLogger("gnw.crawl")


@dataclass
class FileJob:
    url: str
    role: str  # provider | plan
    seed: IndexSeed


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _record(
    store: EvidenceStore,
    snapshot: str,
    role: str,
    result: FetchResult,
    seed: IndexSeed,
    index_url: str | None,
    extra: dict | None = None,
) -> None:
    if result.ok and result.tmp_path is not None:
        store.add_blob(result.sha256, result.tmp_path)
    elif result.tmp_path is not None:
        result.tmp_path.unlink(missing_ok=True)
    store.append(
        ManifestRow(
            snapshot=snapshot,
            role=role,
            url=result.url,
            index_url=index_url,
            issuer_ids=seed.issuer_ids,
            states=seed.states,
            fetched_at=_now(),
            status=result.status,
            sha256=result.sha256 if result.ok else None,
            bytes_content=result.bytes_content,
            content_type=result.content_type,
            last_modified=result.last_modified,
            etag=result.etag,
            final_url=result.final_url,
            was_gzip_payload=result.was_gzip_payload,
            elapsed_s=result.elapsed_s,
            error=result.error,
            extra=extra or {},
        )
    )


def _parse_index(store: EvidenceStore, sha256: str) -> tuple[list[str], list[str]]:
    try:
        doc = store.read_json(sha256)
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("index blob %s unparseable: %s", sha256[:12], exc)
        return [], []
    providers = doc.get("provider_urls") or []
    plans = doc.get("plan_urls") or []
    return (
        [u for u in providers if isinstance(u, str)],
        [u for u in plans if isinstance(u, str)],
    )


def crawl(
    store: EvidenceStore,
    seeds: list[IndexSeed],
    snapshot: str,
    tmp_dir: Path,
    workers: int = 8,
    limit_files_per_index: int | None = None,
    roles: tuple[str, ...] = ("provider", "plan"),
) -> dict:
    fetcher = PoliteFetcher(tmp_dir)
    done = store.fetched_ok_urls(snapshot)  # idempotent resume
    stats = {"indexes": 0, "index_errors": 0, "files": 0, "file_errors": 0, "skipped": 0}

    # Phase 1: indexes.
    jobs: list[FileJob] = []

    def do_index(seed: IndexSeed) -> None:
        if seed.url in done:
            existing = next(
                r
                for r in store.load_manifest(snapshot)
                if r["url"] == seed.url and r.get("sha256")
            )
            sha = existing["sha256"]
            stats["skipped"] += 1
        else:
            result = fetcher.fetch(seed.url)
            provider_urls: list[str] = []
            plan_urls: list[str] = []
            if result.ok:
                stats["indexes"] += 1
            else:
                stats["index_errors"] += 1
                log.warning("index %s -> %s %s", seed.url, result.status, result.error)
            sha = result.sha256
            _record(store, snapshot, "index", result, seed, None)
            if not result.ok:
                return
        provider_urls, plan_urls = _parse_index(store, sha)
        if limit_files_per_index is not None:
            provider_urls = provider_urls[:limit_files_per_index]
            plan_urls = plan_urls[:limit_files_per_index]
        if "provider" in roles:
            jobs.extend(FileJob(u, "provider", seed) for u in provider_urls)
        if "plan" in roles:
            jobs.extend(FileJob(u, "plan", seed) for u in plan_urls)

    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(do_index, seeds))

    # Phase 2: declared files. De-dupe URLs shared across indexes.
    seen: set[str] = set()
    unique_jobs = []
    for job in jobs:
        if job.url in seen:
            continue
        seen.add(job.url)
        if job.url in done:
            stats["skipped"] += 1
            continue
        unique_jobs.append(job)
    log.info("file jobs: %d unique (%d skipped as already fetched)", len(unique_jobs), stats["skipped"])

    def do_file(job: FileJob) -> None:
        result = fetcher.fetch(job.url)
        if result.ok:
            stats["files"] += 1
        else:
            stats["file_errors"] += 1
            log.warning("%s %s -> %s %s", job.role, job.url, result.status, result.error)
        _record(store, snapshot, job.role, result, job.seed, job.seed.url)

    with ThreadPoolExecutor(workers) as pool:
        list(pool.map(do_file, unique_jobs))

    return stats
