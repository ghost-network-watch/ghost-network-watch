"""Second pass on CareSource WI file: profile the 866-389-2727 cohort.

- address-state distribution of cohort vs whole file
- how many cohort records have ANY Wisconsin address
- specialty / name profile (MinuteClinic share)
- distinct NPIs, duplicate-record check
- builds the final evidence exhibit
"""
import json, ssl, zlib, re, time
from pathlib import Path
from urllib.request import Request, urlopen
from collections import Counter

OUT = Path(__file__).resolve().parent / "data"
OUT.mkdir(parents=True, exist_ok=True)

CTX = ssl.create_default_context()
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
TARGET = "8663892727"
URL = "https://www.caresource.com/vendor/cms/data/20260820/providers_WI_20260820.json"

hdrs = {"User-Agent": UA, "Accept": "application/json,*/*",
        "Accept-Encoding": "gzip", "Range": "bytes=0-8388607"}
req = Request(URL, headers=hdrs)
with urlopen(req, timeout=60, context=CTX) as resp:
    raw = resp.read(9 * 1024 * 1024)
    if resp.headers.get("Content-Encoding") == "gzip" or raw[:2] == b"\x1f\x8b":
        raw = zlib.decompressobj(47).decompress(raw, 220 * 1024 * 1024)
recs = json.loads(raw.decode("utf-8", errors="replace"))
print("records:", len(recs))

def digits(x): return re.sub(r"\D", "", str(x or ""))
def name_of(r):
    n = r.get("name")
    if isinstance(n, dict):
        return " ".join(x for x in (n.get("first"), n.get("middle"), n.get("last")) if x).strip()
    return n or r.get("group_name") or r.get("facility_name") or ""

cohort = []          # records containing TARGET
all_addr_states = Counter()
rec_has_wi_addr = 0
npi_counter = Counter()
for r in recs:
    npi_counter[str(r.get("npi"))] += 1
    addrs = [a for a in (r.get("addresses") or []) if isinstance(a, dict)]
    states = {str(a.get("state", "")).upper() for a in addrs}
    for a in addrs: all_addr_states[str(a.get("state", "")).upper()] += 1
    if "WI" in states: rec_has_wi_addr += 1
    phones = {digits(a.get("phone")) for a in addrs if digits(a.get("phone"))}
    if TARGET in phones:
        cohort.append((r, phones, states, addrs))

print("whole-file: records with >=1 WI address:", rec_has_wi_addr, "/", len(recs))
print("whole-file top address states:", all_addr_states.most_common(10))
dupes = sum(1 for k, v in npi_counter.items() if v > 1)
print("whole-file distinct NPIs:", len(npi_counter), "| NPIs appearing in >1 record:", dupes)

# cohort profile
co_states = Counter(); co_spec = Counter(); co_names = Counter()
co_wi = 0; only_target = 0; mc_name = 0
co_npis = set()
for r, phones, states, addrs in cohort:
    co_npis.add(str(r.get("npi")))
    for s in states: co_states[s] += 1
    if "WI" in states: co_wi += 1
    if phones == {TARGET}: only_target += 1
    sp = r.get("specialty") or []
    if isinstance(sp, str): sp = [sp]
    for s in sp: co_spec[str(s)] += 1
    nm = name_of(r)
    co_names[nm] += 1
    if re.search(r"minute\s*clinic", nm, re.I): mc_name += 1

print()
print("COHORT (records listing 866-389-2727 on >=1 address):", len(cohort))
print("  distinct NPIs:", len(co_npis))
print("  only_target (no other phone):", only_target)
print("  records w/ >=1 WI address:", co_wi)
print("  record-level state footprint:", co_states.most_common(15))
print("  MinuteClinic-named records:", mc_name)
print("  top specialties:", co_spec.most_common(8))
print("  top names:", co_names.most_common(10))

# address-level for cohort
co_addr_states = Counter(); n_addr_total = 0
for r, phones, states, addrs in cohort:
    for a in addrs:
        co_addr_states[str(a.get("state","")).upper()] += 1; n_addr_total += 1
print("  cohort address-level states:", co_addr_states.most_common(15), "total addrs:", n_addr_total)

# NPPES cross-check on 5 cohort individuals (polite: 5 calls, public API)
import urllib.request
checks = []
seen = set()
for r, phones, states, addrs in cohort:
    if r.get("type") == "INDIVIDUAL" and phones == {TARGET} and str(r.get("npi")) not in seen:
        seen.add(str(r.get("npi")))
        if len(checks) >= 5: break
        npi = str(r.get("npi"))
        try:
            u = f"https://npiregistry.cms.hhs.gov/api/?version=2.1&number={npi}"
            with urlopen(Request(u, headers={"User-Agent": UA}), timeout=30, context=CTX) as resp2:
                nj = json.loads(resp2.read())
            res = (nj.get("results") or [{}])[0]
            loc = next((a for a in res.get("addresses", []) if a.get("address_purpose") == "LOCATION"), {})
            checks.append({
                "npi": npi,
                "caresource_name": name_of(r),
                "caresource_states": sorted(states),
                "caresource_phones": sorted(phones),
                "nppes_name": f'{res.get("basic",{}).get("first_name","")} {res.get("basic",{}).get("last_name","")}'.strip(),
                "nppes_location_city_state": f'{loc.get("city","")}, {loc.get("state","")}',
                "nppes_phone": loc.get("telephone_number"),
                "nppes_primary_taxonomy": next((t.get("desc") for t in res.get("taxonomies", []) if t.get("primary")), None),
            })
            time.sleep(1)
        except Exception as e:
            checks.append({"npi": npi, "error": str(e)[:120]})
print()
print(json.dumps(checks, indent=1))

json.dump({
    "cohort_n": len(cohort), "cohort_distinct_npis": len(co_npis),
    "cohort_only_target": only_target, "cohort_with_wi_address": co_wi,
    "cohort_record_state_footprint": dict(co_states),
    "cohort_addr_level_states": dict(co_addr_states),
    "cohort_minuteclinic_named": mc_name,
    "cohort_top_specialties": co_spec.most_common(10),
    "whole_file_records": len(recs),
    "whole_file_records_with_wi_address": rec_has_wi_addr,
    "whole_file_addr_states_top": all_addr_states.most_common(15),
    "whole_file_distinct_npis": len(npi_counter),
    "nppes_spot_checks": checks,
}, open(OUT / "caresource_wi_cohort.json", "w"), indent=1)
print("saved data/caresource_wi_cohort.json")
