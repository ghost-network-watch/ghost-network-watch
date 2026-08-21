"""Streaming parse: raw blobs -> Parquet tables.

Provider files reach 1GB decompressed (Moda OR), so records stream through
ijson and flush to Parquet in batches — nothing holds a whole file in memory.
One Parquet part per source blob keeps the stage resumable and parallel-safe:
a part that exists is done.

Tables (all rows carry source_sha256 + record_idx so every derived fact can
be traced back to the exact evidence blob):
  providers          one row per provider record
  provider_addresses one row per address on a record
  provider_plans     one row per plan membership on a record
  plans              one row per plan record (from plan-role blobs)
Parse failures land in parse_log.jsonl — an unparseable mandated file is a
finding, same rule as the crawler.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

import ijson
import pyarrow as pa
import pyarrow.parquet as pq

from .store import EvidenceStore

log = logging.getLogger("gnw.parse")

_BATCH = 50_000
_SEP = "|"

PROVIDERS_SCHEMA = pa.schema(
    [
        ("snapshot", pa.string()),
        ("source_sha256", pa.string()),
        ("record_idx", pa.int64()),
        ("npi", pa.string()),
        ("type", pa.string()),
        ("name_first", pa.string()),
        ("name_middle", pa.string()),
        ("name_last", pa.string()),
        ("name_suffix", pa.string()),
        ("facility_name", pa.string()),
        ("facility_types", pa.string()),
        ("gender", pa.string()),
        ("accepting", pa.string()),
        ("last_updated_on", pa.string()),
        ("specialties", pa.string()),
        ("specialty_count", pa.int32()),
        ("languages", pa.string()),
        ("addresses_count", pa.int32()),
        ("plans_count", pa.int32()),
    ]
)

ADDRESSES_SCHEMA = pa.schema(
    [
        ("snapshot", pa.string()),
        ("source_sha256", pa.string()),
        ("record_idx", pa.int64()),
        ("addr_idx", pa.int32()),
        ("address", pa.string()),
        ("address_2", pa.string()),
        ("city", pa.string()),
        ("state", pa.string()),
        ("zip", pa.string()),
        ("phone", pa.string()),
    ]
)

PROVIDER_PLANS_SCHEMA = pa.schema(
    [
        ("snapshot", pa.string()),
        ("source_sha256", pa.string()),
        ("record_idx", pa.int64()),
        ("plan_idx", pa.int32()),
        ("plan_id_type", pa.string()),
        ("plan_id", pa.string()),
        ("network_tier", pa.string()),
        ("years", pa.string()),
    ]
)

PLANS_SCHEMA = pa.schema(
    [
        ("snapshot", pa.string()),
        ("source_sha256", pa.string()),
        ("record_idx", pa.int64()),
        ("plan_id_type", pa.string()),
        ("plan_id", pa.string()),
        ("marketing_name", pa.string()),
        ("summary_url", pa.string()),
        ("network_tiers", pa.string()),
        ("last_updated_on", pa.string()),
    ]
)


def _s(value) -> str | None:
    """Stringify scalars; None for missing. Keeps raw values raw."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float, bool)):
        return str(value)
    return json.dumps(value, separators=(",", ":"))


def _joined(value) -> tuple[str | None, int]:
    if value is None:
        return None, 0
    if isinstance(value, str):
        return value, 1
    if isinstance(value, list):
        items = [str(x) for x in value if x not in (None, "")]
        return (_SEP.join(items) or None), len(items)
    return _s(value), 1


class _PartWriter:
    """Buffered writer for one (table, source blob) Parquet part."""

    def __init__(self, path: Path, schema: pa.Schema) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._tmp = path.with_suffix(".parquet.tmp")
        self._final = path
        self._schema = schema
        self._writer = pq.ParquetWriter(self._tmp, schema, compression="zstd")
        self._rows: list[dict] = []
        self.count = 0

    def add(self, row: dict) -> None:
        self._rows.append(row)
        self.count += 1
        if len(self._rows) >= _BATCH:
            self._flush()

    def _flush(self) -> None:
        if self._rows:
            self._writer.write_table(
                pa.Table.from_pylist(self._rows, schema=self._schema)
            )
            self._rows = []

    def close(self) -> None:
        self._flush()
        self._writer.close()
        self._tmp.rename(self._final)

    def abort(self) -> None:
        self._writer.close()
        self._tmp.unlink(missing_ok=True)


@dataclass
class ParseStats:
    blobs: int = 0
    skipped: int = 0
    failed: int = 0
    provider_records: int = 0
    plan_records: int = 0


def _iter_records(fh, top_key: str):
    """Stream records from a JSON array, tolerating an object wrapper.

    Decodes with errors='replace': several issuers publish mandated JSON
    containing raw Latin-1 bytes (e.g. "Blue Cross\\xae"), which is an
    encoding violation worth flagging but not worth losing the file over.
    The crawler-side blob keeps the original bytes as evidence.
    """
    first = fh.read(1)
    while first.isspace():
        first = fh.read(1)
    fh.seek(0)
    prefix = "item" if first == b"[" else f"{top_key}.item"
    import io

    text = io.TextIOWrapper(fh, encoding="utf-8", errors="replace")
    yield from ijson.items(text, prefix)


def _parse_provider_blob(
    store: EvidenceStore, sha256: str, snapshot: str, out_root: Path
) -> int:
    key = sha256[:16]
    prov = _PartWriter(out_root / "providers" / f"{key}.parquet", PROVIDERS_SCHEMA)
    addr = _PartWriter(out_root / "provider_addresses" / f"{key}.parquet", ADDRESSES_SCHEMA)
    pplan = _PartWriter(out_root / "provider_plans" / f"{key}.parquet", PROVIDER_PLANS_SCHEMA)
    writers = (prov, addr, pplan)
    base = {"snapshot": snapshot, "source_sha256": sha256}
    try:
        with store.open_blob(sha256) as fh:
            for idx, rec in enumerate(_iter_records(fh, "providers")):
                if not isinstance(rec, dict):
                    continue
                name = rec.get("name") or {}
                if not isinstance(name, dict):
                    name = {}
                specialties, spec_n = _joined(rec.get("specialty"))
                languages, _ = _joined(rec.get("languages"))
                facility_types, _ = _joined(rec.get("facility_type"))
                addresses = rec.get("addresses")
                addresses = addresses if isinstance(addresses, list) else []
                plans = rec.get("plans")
                plans = plans if isinstance(plans, list) else []

                prov.add(
                    {
                        **base,
                        "record_idx": idx,
                        "npi": _s(rec.get("npi")),
                        "type": _s(rec.get("type")),
                        "name_first": _s(name.get("first")),
                        "name_middle": _s(name.get("middle")),
                        "name_last": _s(name.get("last")),
                        "name_suffix": _s(name.get("suffix")),
                        "facility_name": _s(rec.get("facility_name")),
                        "facility_types": facility_types,
                        # Oscar publishes `gender`, others `sex`.
                        "gender": _s(rec.get("gender") if rec.get("gender") is not None else rec.get("sex")),
                        "accepting": _s(rec.get("accepting")),
                        "last_updated_on": _s(rec.get("last_updated_on")),
                        "specialties": specialties,
                        "specialty_count": spec_n,
                        "languages": languages,
                        "addresses_count": len(addresses),
                        "plans_count": len(plans),
                    }
                )
                for a_idx, a in enumerate(addresses):
                    if not isinstance(a, dict):
                        continue
                    addr.add(
                        {
                            **base,
                            "record_idx": idx,
                            "addr_idx": a_idx,
                            "address": _s(a.get("address")),
                            "address_2": _s(a.get("address_2")),
                            "city": _s(a.get("city")),
                            "state": _s(a.get("state")),
                            "zip": _s(a.get("zip")),
                            "phone": _s(a.get("phone")),
                        }
                    )
                for p_idx, p in enumerate(plans):
                    if not isinstance(p, dict):
                        continue
                    years, _ = _joined(p.get("years"))
                    pplan.add(
                        {
                            **base,
                            "record_idx": idx,
                            "plan_idx": p_idx,
                            "plan_id_type": _s(p.get("plan_id_type")),
                            "plan_id": _s(p.get("plan_id")),
                            "network_tier": _s(p.get("network_tier")),
                            "years": years,
                        }
                    )
    except Exception:
        for w in writers:
            w.abort()
        raise
    count = prov.count
    for w in writers:
        w.close()
    return count


def _parse_plan_blob(
    store: EvidenceStore, sha256: str, snapshot: str, out_root: Path
) -> int:
    key = sha256[:16]
    writer = _PartWriter(out_root / "plans" / f"{key}.parquet", PLANS_SCHEMA)
    try:
        with store.open_blob(sha256) as fh:
            for idx, rec in enumerate(_iter_records(fh, "plans")):
                if not isinstance(rec, dict):
                    continue
                network = rec.get("network")
                tiers = None
                if isinstance(network, list):
                    tiers = _SEP.join(
                        str(n.get("network_tier"))
                        for n in network
                        if isinstance(n, dict) and n.get("network_tier")
                    ) or None
                writer.add(
                    {
                        "snapshot": snapshot,
                        "source_sha256": sha256,
                        "record_idx": idx,
                        "plan_id_type": _s(rec.get("plan_id_type")),
                        "plan_id": _s(rec.get("plan_id")),
                        "marketing_name": _s(rec.get("marketing_name")),
                        "summary_url": _s(rec.get("summary_url")),
                        "network_tiers": tiers,
                        "last_updated_on": _s(rec.get("last_updated_on")),
                    }
                )
    except Exception:
        writer.abort()
        raise
    count = writer.count
    writer.close()
    return count


def parse_snapshot(
    store: EvidenceStore,
    snapshot: str,
    parquet_root: Path,
    limit: int | None = None,
) -> ParseStats:
    out_root = parquet_root / snapshot
    out_root.mkdir(parents=True, exist_ok=True)
    parse_log = out_root / "parse_log.jsonl"
    stats = ParseStats()

    # Latest successful fetch per URL wins; dedupe blobs shared across URLs.
    todo: dict[str, str] = {}  # sha256 -> role
    for row in store.load_manifest(snapshot):
        if row.get("sha256") and row.get("role") in ("provider", "plan"):
            todo[row["sha256"]] = row["role"]

    items = sorted(todo.items())
    if limit is not None:
        items = items[:limit]
    for sha256, role in items:
        table = "providers" if role == "provider" else "plans"
        part = out_root / table / f"{sha256[:16]}.parquet"
        if part.exists():
            stats.skipped += 1
            continue
        try:
            if role == "provider":
                n = _parse_provider_blob(store, sha256, snapshot, out_root)
                stats.provider_records += n
            else:
                n = _parse_plan_blob(store, sha256, snapshot, out_root)
                stats.plan_records += n
            stats.blobs += 1
            log.info("parsed %s %s: %d records", role, sha256[:12], n)
        except Exception as exc:  # noqa: BLE001 — failures are findings
            stats.failed += 1
            log.warning("parse failed %s %s: %s", role, sha256[:12], exc)
            with open(parse_log, "a") as fh:
                fh.write(
                    json.dumps(
                        {
                            "sha256": sha256,
                            "role": role,
                            "error": f"{type(exc).__name__}: {exc}"[:400],
                        }
                    )
                    + "\n"
                )
    return stats
