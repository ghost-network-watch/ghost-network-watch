"""Crawl seeds: the CMS Machine-Readable URL PUF, scoped to medical issuers.

Scope classification (medical vs dental-only) comes from the scoping pass's
hosting-platform landscape (scoping/evidence/hosting_platform_landscape.json).
Hosts not present in the landscape default to IN scope — better to crawl a
dental straggler than silently skip a medical issuer.
"""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

REPO_ROOT = Path(__file__).resolve().parents[2]
PUF_CSV = REPO_ROOT / "scoping" / "data" / "mr-puf-2026.csv"
PLATFORM_LANDSCAPE = (
    REPO_ROOT / "scoping" / "evidence" / "hosting_platform_landscape.json"
)


@dataclass
class IndexSeed:
    url: str
    issuer_ids: list[str] = field(default_factory=list)
    states: list[str] = field(default_factory=list)
    scope: str = "medical"  # medical | dental
    platform: str = "unknown"


def _host(url: str) -> str:
    return urlparse(url).netloc.lower()


def _host_classification() -> dict[str, tuple[str, str]]:
    """host -> (medical|dental, platform name)."""
    landscape = json.loads(PLATFORM_LANDSCAPE.read_text())
    out: dict[str, tuple[str, str]] = {}
    for platform in landscape["platforms"]:
        cls = platform["dental_or_medical"]
        for host in platform["hosts"]:
            out[host.lower()] = (cls, platform["platform"])
    return out


def load_seeds(puf_csv: Path = PUF_CSV) -> list[IndexSeed]:
    classification = _host_classification()
    by_url: dict[str, IndexSeed] = {}
    with open(puf_csv) as fh:
        for row in csv.DictReader(fh):
            url = row["URL Submitted"].strip()
            seed = by_url.get(url)
            if seed is None:
                cls, platform = classification.get(_host(url), ("medical", "unknown"))
                seed = by_url[url] = IndexSeed(url=url, scope=cls, platform=platform)
            seed.issuer_ids.append(row["Issuer ID"])
            if row["State"] not in seed.states:
                seed.states.append(row["State"])
    return sorted(by_url.values(), key=lambda s: s.url)


def medical_seeds(puf_csv: Path = PUF_CSV) -> list[IndexSeed]:
    return [s for s in load_seeds(puf_csv) if s.scope == "medical"]
