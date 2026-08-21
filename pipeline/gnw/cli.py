"""gnw command line.

  python -m gnw.cli seed                          # show crawl scope
  python -m gnw.cli crawl --snapshot 2026-08 \
      [--include-host H ...] [--limit-files N] [--workers 8] [--all-scopes]
  python -m gnw.cli status --snapshot 2026-08     # manifest summary
"""

from __future__ import annotations

import argparse
import logging
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

from .crawl import crawl
from .seed import load_seeds, medical_seeds
from .store import EvidenceStore

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = REPO_ROOT / "data"


def cmd_seed(_args) -> None:
    seeds = load_seeds()
    by_scope = Counter(s.scope for s in seeds)
    print(f"index URLs: {len(seeds)}  scope: {dict(by_scope)}")
    medical = [s for s in seeds if s.scope == "medical"]
    rows = sum(len(s.issuer_ids) for s in medical)
    states = sorted({st for s in medical for st in s.states})
    print(f"medical: {len(medical)} indexes, {rows} issuer rows, {len(states)} states")
    for s in medical:
        print(f"  {','.join(s.states):12s} {len(s.issuer_ids):2d} issuers  {s.url}")


def cmd_crawl(args) -> None:
    seeds = load_seeds() if args.all_scopes else medical_seeds()
    if args.include_host:
        wanted = {h.lower() for h in args.include_host}
        seeds = [s for s in seeds if urlparse(s.url).netloc.lower() in wanted]
    if not seeds:
        raise SystemExit("no seeds match the filters")
    store = EvidenceStore(DATA_ROOT)
    print(f"crawling {len(seeds)} indexes -> snapshot {args.snapshot}")
    stats = crawl(
        store,
        seeds,
        snapshot=args.snapshot,
        tmp_dir=DATA_ROOT / "tmp",
        workers=args.workers,
        limit_files_per_index=args.limit_files,
    )
    print(f"done: {stats}")


def cmd_parse(args) -> None:
    from .parse import parse_snapshot

    store = EvidenceStore(DATA_ROOT)
    stats = parse_snapshot(
        store,
        snapshot=args.snapshot,
        parquet_root=DATA_ROOT / "parquet",
        limit=args.limit,
    )
    print(
        f"parsed {stats.blobs} blobs ({stats.skipped} already done, {stats.failed} failed): "
        f"{stats.provider_records} provider records, {stats.plan_records} plan records"
    )


def cmd_refs(args) -> None:
    from .reference import BUILDERS, ReferenceStore

    store = ReferenceStore(DATA_ROOT / "reference")
    names = list(BUILDERS) if args.source == "all" else [args.source]
    for name in names:
        print(f"building reference: {name}")
        BUILDERS[name](store)


def cmd_compact(args) -> None:
    from .compact import build_compact

    build_compact(DATA_ROOT, snapshot=args.snapshot)


def cmd_score(args) -> None:
    from .scoring import build_scores

    build_scores(DATA_ROOT, snapshot=args.snapshot)


def cmd_site(args) -> None:
    from .site import build_site

    build_site(
        DATA_ROOT,
        snapshot=args.snapshot,
        repo_root=REPO_ROOT,
        out_dir=Path(args.out) if args.out else REPO_ROOT / "site" / "dist",
        wa_kit=Path(args.wa_kit).expanduser(),
    )


def cmd_notify(args) -> None:
    from .notify import build_notifications

    build_notifications(
        DATA_ROOT, snapshot=args.snapshot,
        publish_date=args.publish_date, only_issuer=args.issuer,
    )


def cmd_flags(args) -> None:
    from .flags import FlagEngine

    engine = FlagEngine(DATA_ROOT, snapshot=args.snapshot, fetch_date=args.fetch_date)
    results = engine.run(only=args.metric)
    for metric, n in results.items():
        print(f"{metric}: {n:,} evidence rows")


def cmd_status(args) -> None:
    store = EvidenceStore(DATA_ROOT)
    rows = store.load_manifest(args.snapshot)
    if not rows:
        print("no manifest for snapshot", args.snapshot)
        return
    by_role = Counter(r["role"] for r in rows)
    ok = [r for r in rows if r.get("sha256")]
    errs = [r for r in rows if r.get("error") or (r.get("status") or 0) != 200]
    total_bytes = sum(r["bytes_content"] for r in ok)
    print(f"snapshot {args.snapshot}: {len(rows)} fetches  roles={dict(by_role)}")
    print(f"  ok: {len(ok)}  ({total_bytes/1e9:.2f} GB content)")
    print(f"  failures: {len(errs)}")
    for r in errs[:20]:
        print(f"    {r['role']:8s} {r.get('status')} {r.get('error') or ''}  {r['url'][:80]}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="gnw")
    sub = parser.add_subparsers(required=True)

    p = sub.add_parser("seed", help="show crawl scope from the PUF + platform map")
    p.set_defaults(func=cmd_seed)

    p = sub.add_parser("crawl", help="fetch indexes and their declared files")
    p.add_argument("--snapshot", required=True, help="e.g. 2026-08")
    p.add_argument("--include-host", action="append", help="limit to these index hosts")
    p.add_argument("--limit-files", type=int, help="max provider/plan files per index")
    p.add_argument("--workers", type=int, default=8)
    p.add_argument("--all-scopes", action="store_true", help="include dental issuers")
    p.set_defaults(func=cmd_crawl)

    p = sub.add_parser("parse", help="parse fetched blobs into Parquet tables")
    p.add_argument("--snapshot", required=True)
    p.add_argument("--limit", type=int, help="max blobs to parse this run")
    p.set_defaults(func=cmd_parse)

    p = sub.add_parser("refs", help="download + convert reference datasets")
    p.add_argument(
        "--source",
        default="all",
        choices=["all", "nppes", "pufs", "landscape", "nucc", "zcta", "adjacency"],
    )
    p.set_defaults(func=cmd_refs)

    p = sub.add_parser("compact", help="build integer-encoded join tables")
    p.add_argument("--snapshot", required=True)
    p.set_defaults(func=cmd_compact)

    p = sub.add_parser("score", help="aggregate evidence rows into plan-county scores")
    p.add_argument("--snapshot", required=True)
    p.set_defaults(func=cmd_score)

    p = sub.add_parser("site", help="generate the static site + open data exports")
    p.add_argument("--snapshot", required=True)
    p.add_argument("--out", help="output dir (default site/dist)")
    p.add_argument("--wa-kit", default="~/soorena.io/webawesome",
                   help="path to a Web Awesome kit to copy (not committed)")
    p.set_defaults(func=cmd_site)

    p = sub.add_parser("notify", help="generate issuer pre-notification bundles (no sending)")
    p.add_argument("--snapshot", required=True)
    p.add_argument("--publish-date", required=True, help="planned publication date, YYYY-MM-DD")
    p.add_argument("--issuer", help="single HIOS issuer id")
    p.set_defaults(func=cmd_notify)

    p = sub.add_parser("flags", help="run rubric metrics -> evidence rows")
    p.add_argument("--snapshot", required=True)
    p.add_argument("--fetch-date", default="2026-08-21", help="crawl date, YYYY-MM-DD")
    p.add_argument("--metric", help="run a single metric (m3..m10, feed)")
    p.set_defaults(func=cmd_flags)

    p = sub.add_parser("status", help="summarize a snapshot manifest")
    p.add_argument("--snapshot", required=True)
    p.set_defaults(func=cmd_status)

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")
    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
