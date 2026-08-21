"""CareSource duplicate-phone audit: quantify 866-389-2727 saturation per state file.

Fetches the 4 CareSource state provider files (Range-limited, gzip; <=8MB transfer each),
classifies every parsed record by phone content, breaks out by state / record type /
plan-network / behavioral-health specialty, and dumps a small evidence exhibit.
"""
import json, ssl, zlib, re, sys, time
from urllib.request import Request, urlopen
from collections import Counter, defaultdict

CTX = ssl.create_default_context()
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
RANGE_CAP = 8 * 1024 * 1024          # bytes transferred per file (gzipped)
DECOMP_CAP = 220 * 1024 * 1024
TARGET = "8663892727"
# Tight BH regex (excludes PT/OT/speech): psych/mental/behavioral/counselor/social work/addiction/substance/marriage-family
BH_PAT = re.compile(r'psych|mental|behav|counsel|cnslr|social work|addiction|substance|marriage', re.I)

FILES = {
    "IN": "https://www.caresource.com/vendor/cms/data/20260820/providers_IN_20260820.json",
    "OH": "https://www.caresource.com/vendor/cms/data/20260820/providers_OH_20260820.json",
    "WI": "https://www.caresource.com/vendor/cms/data/20260820/providers_WI_20260820.json",
    "WV": "https://www.caresource.com/vendor/cms/data/20260820/providers_WV_20260820.json",
}

def fetch(url):
    hdrs = {"User-Agent": UA, "Accept": "application/json,*/*",
            "Accept-Encoding": "gzip", "Range": f"bytes=0-{RANGE_CAP-1}"}
    req = Request(url, headers=hdrs)
    with urlopen(req, timeout=60, context=CTX) as resp:
        raw = resp.read(RANGE_CAP + 1024)
        if resp.headers.get("Content-Encoding") == "gzip" or raw[:2] == b"\x1f\x8b":
            d = zlib.decompressobj(47)
            try:
                raw = d.decompress(raw, DECOMP_CAP)
            except Exception:
                pass
        return raw.decode("utf-8", errors="replace"), resp.status

def parse_records(body):
    body = body.lstrip("﻿ \n\r\t")
    try:
        j = json.loads(body)
        return (j if isinstance(j, list) else [j]), False
    except Exception:
        pass
    if body.startswith("["):
        depth = 0; in_str = False; esc = False; last_end = -1
        for i, ch in enumerate(body):
            if esc: esc = False; continue
            if ch == "\\": esc = True; continue
            if ch == '"': in_str = not in_str; continue
            if in_str: continue
            if ch in "[{": depth += 1
            elif ch in "]}":
                depth -= 1
                if depth == 1 and ch == "}":
                    last_end = i
        if last_end > 0:
            try:
                return json.loads(body[:last_end+1] + "]"), True
            except Exception:
                pass
    return [], True

def digits(x):
    return re.sub(r"\D", "", str(x or ""))

def classify(rec):
    """Return (record_phone_set, n_addresses, n_addr_with_phone)."""
    addrs = rec.get("addresses") or []
    phones = set()
    n_with = 0
    for a in addrs:
        if isinstance(a, dict):
            p = digits(a.get("phone"))
            if p:
                phones.add(p); n_with += 1
    return phones, len(addrs), n_with

def spec_list(rec):
    sp = rec.get("specialty") or []
    return [sp] if isinstance(sp, str) else [str(s) for s in sp]

out = {"fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "target_phone": TARGET, "states": {}}
examples = {"only_target": [], "target_plus_other": [], "no_phone": []}

for state, url in FILES.items():
    body, code = fetch(url)
    recs, truncated = parse_records(body)
    c = Counter()
    by_type = defaultdict(Counter)
    bh = Counter()
    plan_break = defaultdict(Counter)   # network tier -> bucket counts (WI only interest)
    phone_counter = Counter()
    for r in recs:
        if not isinstance(r, dict): continue
        phones, n_addr, n_with = classify(r)
        for p in phones: phone_counter[p] += 1
        if not phones:
            bucket = "no_phone"
        elif phones == {TARGET}:
            bucket = "only_target"
        elif TARGET in phones:
            bucket = "target_plus_other"
        else:
            bucket = "other_only"
        c[bucket] += 1
        c["total"] += 1
        rtype = str(r.get("type"))
        by_type[rtype][bucket] += 1
        by_type[rtype]["total"] += 1
        specs = spec_list(r)
        if any(BH_PAT.search(s) for s in specs):
            bh[bucket] += 1; bh["total"] += 1
        # plan/network breakdown
        for pl in (r.get("plans") or []):
            if isinstance(pl, dict):
                key = f'{pl.get("plan_id","?")[:14]}|{pl.get("network_tier","?")}'
                plan_break[key][bucket] += 1
                plan_break[key]["total"] += 1
        # collect examples from WI
        if state == "WI" and bucket in examples and len(examples[bucket]) < 12:
            ex = {
                "state_file": state,
                "npi": r.get("npi"),
                "type": r.get("type"),
                "name": r.get("name") or r.get("group_name") or r.get("facility_name"),
                "specialty": specs[:4],
                "accepting": r.get("accepting"),
                "last_updated_on": r.get("last_updated_on"),
                "n_addresses": n_addr,
                "distinct_phones": sorted(phones)[:5],
                "sample_addresses": [
                    {k: a.get(k) for k in ("address", "city", "state", "zip", "phone")}
                    for a in (r.get("addresses") or [])[:2] if isinstance(a, dict)
                ],
                "plan_ids": sorted({str(pl.get("plan_id")) for pl in (r.get("plans") or []) if isinstance(pl, dict)})[:4],
            }
            examples[bucket].append(ex)
    st = {
        "url": url, "http_status": code,
        "decompressed_chars": len(body),
        "parse_truncated": truncated,
        "records_parsed": c["total"],
        "buckets": {k: c[k] for k in ("only_target", "target_plus_other", "other_only", "no_phone")},
        "pct_only_target": round(100.0 * c["only_target"] / c["total"], 2) if c["total"] else None,
        "pct_no_direct_phone": round(100.0 * (c["only_target"] + c["no_phone"]) / c["total"], 2) if c["total"] else None,
        "by_type": {t: dict(v) for t, v in by_type.items()},
        "bh_subset": dict(bh),
        "bh_pct_only_target": round(100.0 * bh["only_target"] / bh["total"], 2) if bh["total"] else None,
        "top_phones": phone_counter.most_common(6),
        "n_plan_network_keys": len(plan_break),
    }
    # only keep plan breakdown if the target phone actually shows up
    if c["only_target"] or c["target_plus_other"]:
        st["plan_breakdown"] = {
            k: {"total": v["total"], "only_target": v["only_target"],
                "pct_only_target": round(100.0 * v["only_target"] / v["total"], 1)}
            for k, v in sorted(plan_break.items())[:12]
        }
    out["states"][state] = st
    print(f"{state}: {c['total']} recs (trunc={truncated}) only_target={c['only_target']} "
          f"({st['pct_only_target']}%) target_plus_other={c['target_plus_other']} "
          f"no_phone={c['no_phone']}", flush=True)
    time.sleep(2)

out["wi_examples"] = examples
with open("/Users/soorena/ghost-network-watch/scoping/data/caresource_phone_audit.json", "w") as f:
    json.dump(out, f, indent=1)
print("saved data/caresource_phone_audit.json")
