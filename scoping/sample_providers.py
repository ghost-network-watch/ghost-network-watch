"""Sample provider files from each CMS machine-readable index; emit per-index quality report."""
import json, ssl, zlib, csv, hashlib, re, sys
from urllib.request import Request, urlopen
from urllib.parse import urlparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

CTX = ssl.create_default_context()
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
RANGE_CAP = 8 * 1024 * 1024
FULL_CAP = 30 * 1024 * 1024
BH_PAT = re.compile(r'psych|mental|behavior|counsel|social work|therap|addiction|substance|marriage|lcsw|lmft|lpc', re.I)

def fetch(url, use_range=False):
    hdrs = {"User-Agent": UA, "Accept": "application/json,*/*", "Accept-Encoding": "gzip"}
    if use_range:
        hdrs["Range"] = f"bytes=0-{RANGE_CAP-1}"
    req = Request(url, headers=hdrs)
    with urlopen(req, timeout=40, context=CTX) as resp:
        raw = resp.read(FULL_CAP)
        enc = resp.headers.get("Content-Encoding", "")
        truncated_http = len(raw) >= (RANGE_CAP if use_range and resp.status == 206 else FULL_CAP)
        if enc == "gzip" or raw[:2] == b"\x1f\x8b":
            d = zlib.decompressobj(47)
            try:
                raw = d.decompress(raw, 200 * 1024 * 1024)
            except Exception:
                pass
        return raw.decode("utf-8", errors="replace"), resp.status, truncated_http

def parse_records(body):
    """Parse possibly-truncated JSON array of provider objects."""
    body = body.lstrip("﻿ \n\r\t")
    try:
        j = json.loads(body)
        if isinstance(j, list): return j, False
        if isinstance(j, dict):
            for k in ("providers", "provider", "data"):
                if isinstance(j.get(k), list): return j[k], False
            return [j], False
    except Exception:
        pass
    # truncated: depth-aware scan for last complete top-level array element
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

def luhn_npi(npi):
    s = "80840" + str(npi)
    if not str(npi).isdigit() or len(str(npi)) != 10: return False
    total, alt = 0, False
    for ch in reversed(s):
        d = int(ch)
        if alt:
            d *= 2
            if d > 9: d -= 9
        total += d
        alt = not alt
    return total % 10 == 0

def analyze(records):
    n = len(records)
    st = {"n_records": n}
    if not n: return st
    fields = Counter()
    spec = Counter(); accepting = Counter(); years = Counter(); phones = Counter()
    types = Counter(); npi_ok = 0; npi_present = 0; bh = 0; max_addr = 0
    lu_min = lu_max = None
    for r in records[:1000]:
        if not isinstance(r, dict): continue
        for k in r: fields[k] += 1
        types[str(r.get("type"))] += 1
        npi = r.get("npi")
        if npi not in (None, ""):
            npi_present += 1
            if luhn_npi(npi): npi_ok += 1
        sp = r.get("specialty") or []
        if isinstance(sp, str): sp = [sp]
        joined = "|".join(str(x) for x in sp)
        for s in sp: spec[str(s).strip()[:60]] += 1
        if BH_PAT.search(joined): bh += 1
        accepting[str(r.get("accepting"))[:40]] += 1
        lu = r.get("last_updated_on")
        if lu:
            years[str(lu)[:4]] += 1
            lu_min = min(lu_min or lu, lu); lu_max = max(lu_max or lu, lu)
        addrs = r.get("addresses") or []
        if isinstance(addrs, list):
            max_addr = max(max_addr, len(addrs))
            for a in addrs:
                if isinstance(a, dict) and a.get("phone"):
                    phones[re.sub(r"\D", "", str(a["phone"]))] += 1
    st.update({
        "analyzed": min(n, 1000),
        "field_presence": {k: v for k, v in fields.most_common(25)},
        "type_dist": dict(types),
        "npi_present": npi_present, "npi_luhn_valid": npi_ok,
        "bh_matches": bh,
        "unique_specialties": len(spec),
        "top_specialties": spec.most_common(20),
        "bh_specialty_values": [s for s, _ in spec.most_common(500) if BH_PAT.search(s)][:25],
        "accepting_values": dict(accepting),
        "last_updated_min": lu_min, "last_updated_max": lu_max,
        "last_updated_years": dict(years),
        "max_addresses_per_provider": max_addr,
        "top_dup_phones": [(p, c) for p, c in phones.most_common(8) if c > 1],
    })
    return st

def process(item):
    idx_url, meta = item
    rep = {"index_url": idx_url, **meta}
    try:
        body, code, _ = fetch(idx_url)
        idx = json.loads(body)
        purls = idx.get("provider_urls") or []
        rep["provider_urls_count"] = len(purls)
        rep["plan_urls_count"] = len(idx.get("plan_urls") or [])
        picks = purls[:1] if len(purls) == 1 else ([purls[0], purls[len(purls)//2]] if purls else [])
        rep["files"] = []
        for pu in picks:
            f = {"url": pu}
            try:
                pbody, pcode, trunc_http = fetch(pu, use_range=True)
                f["http_code"] = pcode
                f["fetched_chars"] = len(pbody)
                recs, truncated = parse_records(pbody)
                f["truncated_sample"] = truncated or trunc_http
                f["stats"] = analyze(recs)
            except Exception as e:
                f["error"] = f"{type(e).__name__}: {e}"[:200]
            rep["files"].append(f)
    except Exception as e:
        rep["error"] = f"{type(e).__name__}: {e}"[:200]
    h = hashlib.sha1(idx_url.encode()).hexdigest()[:12]
    with open(f"probes/{h}.json", "w") as fp:
        json.dump(rep, fp, indent=1)
    return idx_url, "error" if "error" in rep else "ok"

rows = list(csv.DictReader(open("data/mr-puf-2026.csv")))
by_url = {}
for r in rows:
    u = r["URL Submitted"].strip()
    by_url.setdefault(u, {"states": set(), "issuer_ids": []})
    by_url[u]["states"].add(r["State"]); by_url[u]["issuer_ids"].append(r["Issuer ID"])
items = [(u, {"states": sorted(m["states"]), "issuer_ids": m["issuer_ids"]}) for u, m in sorted(by_url.items())]
print(f"processing {len(items)} unique indexes")
with ThreadPoolExecutor(10) as ex:
    for u, status in ex.map(process, items):
        print(status, u[:80], flush=True)
