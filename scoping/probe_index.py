import json, ssl, gzip, io, sys
from urllib.request import Request, urlopen
from concurrent.futures import ThreadPoolExecutor

CTX = ssl.create_default_context()
UA = "ghost-network-watch-scoping/0.1 (research)"

def probe(url):
    rec = {"url": url}
    try:
        req = Request(url, headers={"User-Agent": UA, "Accept": "application/json,*/*",
                                    "Accept-Encoding": "gzip"})
        with urlopen(req, timeout=25, context=CTX) as resp:
            rec["http_code"] = resp.status
            rec["content_type"] = resp.headers.get("Content-Type", "")
            rec["final_url"] = resp.geturl()
            raw = resp.read(20_000_000)
            if resp.headers.get("Content-Encoding") == "gzip" or raw[:2] == b"\x1f\x8b":
                try: raw = gzip.decompress(raw)
                except Exception: pass
            rec["bytes"] = len(raw)
            body = raw.decode("utf-8", errors="replace")
    except Exception as e:
        rec["http_code"] = "ERR"
        rec["error"] = f"{type(e).__name__}: {e}"[:200]
        return rec
    try:
        j = json.loads(body)
        rec["valid_json"] = True
        if isinstance(j, dict):
            rec["keys"] = sorted(j.keys())
            for k in ("provider_urls", "formulary_urls", "plan_urls", "drug_urls"):
                if isinstance(j.get(k), list):
                    rec[k + "_count"] = len(j[k])
                    rec[k + "_sample"] = j[k][:3]
        else:
            rec["json_type"] = type(j).__name__
            if isinstance(j, list):
                rec["list_len"] = len(j)
                rec["list_sample"] = j[:2]
    except Exception as e:
        rec["valid_json"] = False
        rec["parse_error"] = str(e)[:150]
        rec["body_head"] = body[:250]
    return rec

urls = [l.strip() for l in open("data/probe_urls.txt") if l.strip()]
with ThreadPoolExecutor(12) as ex:
    results = list(ex.map(probe, urls))
with open("data/index_probe_results.jsonl", "w") as f:
    for r in results:
        f.write(json.dumps(r) + "\n")
print("done", len(results))
