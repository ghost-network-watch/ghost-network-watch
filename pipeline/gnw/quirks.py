"""Per-host quirks registry.

Every entry is grounded in a scoping observation (scoping/FINDINGS.md). The
crawler must work with defaults for any host not listed; quirks only tune.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

# UHC / Wellmark / MercyCare 403 non-browser UAs on their mandated files.
BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


@dataclass(frozen=True)
class HostQuirks:
    user_agent: str = BROWSER_UA
    # CHRISTUS IIS returns 406 to Accept: application/json; */* is safe everywhere.
    accept: str = "*/*"
    rate_limit_seconds: float = 1.0
    max_retries: int = 3
    backoff_base_seconds: float = 2.0
    connect_timeout: float = 15.0
    read_timeout: float = 120.0
    # Cap is on DECOMPRESSED bytes. Scoping's ~204MB max was transfer-side and
    # Range-capped; Moda's providers-OR.json exceeds 300MB decompressed. Blobs
    # are stored re-gzipped, so a 1GB JSON costs ~100MB on disk.
    max_bytes: int = 1_000_000_000


_HOST_OVERRIDES: dict[str, dict] = {
    # Dropped transfers mid-stream in scoping; 403'd part of its file series
    # mid-crawl in the 2026-08 run (rate-based WAF) — go slow and retry hard.
    "www22.elevancehealth.com": {"max_retries": 5, "rate_limit_seconds": 5.0},
    # Single provider JSONs >1GB decompressed observed in the 2026-08 crawl.
    "providersearch.medmutual.com": {"max_bytes": 4_000_000_000, "read_timeout": 600.0},
    "www.mclaren.org": {"max_bytes": 4_000_000_000, "read_timeout": 600.0},
    "tools.sanfordhealthplan.com": {"max_bytes": 4_000_000_000, "read_timeout": 600.0},
    "legacy.providerlookuponline.com": {"max_bytes": 4_000_000_000, "read_timeout": 600.0},
    # ESB gateway answers 202 (async generation) on first request; a later
    # request returns 200. Crawler retries via resume on re-run.
    "esbgatewaypub.medica.com:443": {"rate_limit_seconds": 2.0},
    # 302-redirects, ignores Range, exposes no freshness headers — nothing to
    # tune, listed so the behavior is documented where an operator will look.
    "fm.formularynavigator.com": {},
    # Heaviest per-issuer corpus (HCSC TX/OK/MT, 112 files, files to ~200MB).
    "mrfdata.hmhs.com": {"read_timeout": 300.0},
    "api.centene.com": {"read_timeout": 300.0},
}


def quirks_for(url: str) -> HostQuirks:
    host = urlparse(url).netloc.lower()
    return HostQuirks(**_HOST_OVERRIDES.get(host, {}))
