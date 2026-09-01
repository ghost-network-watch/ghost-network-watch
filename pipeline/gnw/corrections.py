"""Load and validate published corrections.

The site promises disputes are published alongside the findings they dispute.
corrections/corrections.json is the source; see that directory's README for the
schema and the workflow.

Validation is strict and fails the build rather than skipping a bad record. A
silently dropped correction is the exact failure this feature exists to prevent:
the promise is that a dispute gets published, so a malformed record must stop the
run and get fixed, not disappear.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("gnw.corrections")

REQUIRED = (
    "id", "received", "source", "issuer_id", "disputes", "role_claimed",
    "issuer_verified", "statement", "redacted", "status",
)
STATUSES = {"open", "accepted", "declined", "resolved_in_crawl"}
ROLES = {"insurer", "provider", "public"}

STATUS_LABEL = {
    "open": "Open, no response yet",
    "accepted": "Accepted, finding withdrawn or amended",
    "declined": "Declined, we disagree",
    "resolved_in_crawl": "No longer reproduces in a later crawl",
}


def load_corrections(repo_root: Path) -> list[dict]:
    path = repo_root / "corrections" / "corrections.json"
    if not path.exists():
        log.warning("no corrections file at %s; publishing an empty page", path)
        return []
    records = json.loads(path.read_text())
    if not isinstance(records, list):
        raise ValueError(f"{path}: expected a list of correction records")

    seen: set[str] = set()
    for i, r in enumerate(records):
        where = f"{path}[{i}]"
        missing = [k for k in REQUIRED if k not in r]
        if missing:
            raise ValueError(f"{where}: missing required field(s): {', '.join(missing)}")
        if r["id"] in seen:
            raise ValueError(f"{where}: duplicate id {r['id']!r}")
        seen.add(r["id"])
        if r["status"] not in STATUSES:
            raise ValueError(
                f"{where}: status {r['status']!r} not one of {sorted(STATUSES)}"
            )
        if r["role_claimed"] not in ROLES:
            raise ValueError(
                f"{where}: role_claimed {r['role_claimed']!r} not one of {sorted(ROLES)}"
            )
        if not str(r["statement"]).strip():
            raise ValueError(f"{where}: statement is empty; nothing would be published")
        if r["status"] != "open" and not (r.get("response") or "").strip():
            raise ValueError(
                f"{where}: status is {r['status']!r} but no response is recorded"
            )
        if r["status"] == "resolved_in_crawl" and not r.get("resolved_snapshot"):
            raise ValueError(f"{where}: resolved_in_crawl needs resolved_snapshot")
        r["status_label"] = STATUS_LABEL[r["status"]]

    records.sort(key=lambda r: (r["received"], r["id"]), reverse=True)
    log.info("corrections: %d record(s)", len(records))
    return records


def by_issuer(records: list[dict]) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for r in records:
        if r.get("issuer_id"):
            out.setdefault(str(r["issuer_id"]), []).append(r)
    return out
