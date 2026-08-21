#!/bin/bash
# Probe one CMS machine-readable index URL: status, timing, JSON structure.
url="$1"
out=$(curl -sL --compressed --connect-timeout 8 --max-time 25 --max-filesize 20000000 \
  -w '\n---META---\n%{http_code}\t%{time_total}\t%{size_download}\t%{content_type}\t%{url_effective}' \
  -A "ghost-network-watch-scoping/0.1 (research; contact on file with CMS PUF)" \
  "$url" 2>/dev/null)
meta=$(echo "$out" | awk '/^---META---$/{f=1;next} f')
body=$(echo "$out" | awk '/^---META---$/{exit} {print}')
python3 - "$url" "$meta" << 'PYEOF' 2>/dev/null
import sys, json
url, meta = sys.argv[1], sys.argv[2]
body = sys.stdin.read()
parts = meta.split('\t') if meta else []
rec = {"url": url, "http_code": parts[0] if parts else "ERR",
       "time_s": parts[1] if len(parts)>1 else "", "bytes": parts[2] if len(parts)>2 else "",
       "content_type": parts[3] if len(parts)>3 else "", "final_url": parts[4] if len(parts)>4 else ""}
try:
    j = json.loads(body)
    rec["valid_json"] = True
    if isinstance(j, dict):
        rec["keys"] = sorted(j.keys())
        for k in ("provider_urls","formulary_urls","plan_urls","drug_urls","index_urls"):
            if k in j and isinstance(j[k], list):
                rec[k+"_count"] = len(j[k])
                rec[k+"_sample"] = j[k][:3]
    else:
        rec["json_type"] = type(j).__name__
except Exception as e:
    rec["valid_json"] = False
    rec["parse_error"] = str(e)[:120]
    rec["body_head"] = body[:200]
print(json.dumps(rec))
PYEOF
