"""Add the mail DNS records for ghostnetworkwatch.org through Porkbun's API.

Eight to eleven records typed by hand into a web form is a lot of chances to
put a value in the wrong field. This does it from a definition instead.

Credentials are read from the environment, or from a file given by
PORKBUN_CREDS (default ~/.porkbun.env) holding two lines:

    PORKBUN_API_KEY=pk1_...
    PORKBUN_SECRET_KEY=sk1_...

The keys are never printed, never logged, and never passed on the command line.
Create them at porkbun.com under Account > API Access, and switch API ACCESS on
for the domain itself (Domain Management > ghostnetworkwatch.org > toggle).

Dry run by default. Nothing is written without --apply.

    python ops/porkbun_dns.py                              # show the plan
    python ops/porkbun_dns.py --proton-dkim T1 T2 T3        # include Proton DKIM
    python ops/porkbun_dns.py --apply                       # write it

Idempotent: existing records with the same type, host and content are left
alone, so re-running after adding the Proton DKIM values only adds what is
missing. Nothing is ever deleted, including the Proton verification TXT, which
has to stay in place permanently.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

API = "https://api.porkbun.com/api/json/v3"
DOMAIN = "ghostnetworkwatch.org"

# SES DKIM tokens for this domain, from the identity created in us-east-1.
SES_DKIM = (
    "jo7io6p5j3u5qdm6lfiwnoivzsmmel3h",
    "gz6x6zzkeieithbshcqyfqgt26bvy56n",
    "z2pe5avk34k2l2m6qdltkgoql7gcxx47",
)


def records(proton_dkim: tuple[str, ...]) -> list[dict]:
    """The full intended zone additions. `name` is the host, blank for apex."""
    out: list[dict] = [
        # --- Proton: inbound mail ---
        {"type": "MX", "name": "", "content": "mail.protonmail.ch", "prio": "10",
         "why": "Proton inbound mail, primary"},
        {"type": "MX", "name": "", "content": "mailsec.protonmail.ch", "prio": "20",
         "why": "Proton inbound mail, secondary"},
        # Apex SPF authorises Proton only. SES sends under the bounce subdomain
        # with its own SPF, so amazonses.com does not belong here.
        {"type": "TXT", "name": "", "content": "v=spf1 include:_spf.protonmail.ch ~all",
         "why": "SPF: Proton may send as this domain"},
        {"type": "TXT", "name": "_dmarc",
         "content": "v=DMARC1; p=quarantine; rua=mailto:contact@ghostnetworkwatch.org",
         "why": "DMARC policy plus aggregate reports"},

        # --- SES: outbound signing for the issuer notifications ---
        *[
            {"type": "CNAME", "name": f"{t}._domainkey",
             "content": f"{t}.dkim.amazonses.com",
             "why": f"SES DKIM key {i + 1} of 3"}
            for i, t in enumerate(SES_DKIM)
        ],
        # Custom MAIL FROM subdomain: keeps SPF aligned for DMARC and keeps
        # bounce handling on our own domain.
        {"type": "MX", "name": "bounce",
         "content": "feedback-smtp.us-east-1.amazonses.com", "prio": "10",
         "why": "SES bounce and complaint return path"},
        {"type": "TXT", "name": "bounce", "content": "v=spf1 include:amazonses.com ~all",
         "why": "SPF for the SES MAIL FROM subdomain"},
    ]
    for i, t in enumerate(proton_dkim):
        out.append({
            "type": "CNAME", "name": f"protonmail{'' if i == 0 else i + 1}._domainkey",
            "content": t, "why": f"Proton DKIM key {i + 1} of 3",
        })
    return out


def creds() -> tuple[str, str]:
    key = os.environ.get("PORKBUN_API_KEY", "")
    secret = os.environ.get("PORKBUN_SECRET_KEY", "")
    if not (key and secret):
        path = Path(os.environ.get("PORKBUN_CREDS", "~/.porkbun.env")).expanduser()
        if path.exists():
            for line in path.read_text().splitlines():
                line = line.strip()
                if line.startswith("PORKBUN_API_KEY="):
                    key = line.split("=", 1)[1].strip().strip("'\"")
                elif line.startswith("PORKBUN_SECRET_KEY="):
                    secret = line.split("=", 1)[1].strip().strip("'\"")
    if not (key and secret):
        sys.exit(
            "No credentials. Put them in ~/.porkbun.env as PORKBUN_API_KEY= and "
            "PORKBUN_SECRET_KEY=, then chmod 600 that file."
        )
    return key, secret


def call(path: str, key: str, secret: str, **body) -> dict:
    payload = {"apikey": key, "secretapikey": secret, **body}
    req = urllib.request.Request(
        f"{API}/{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=45) as r:
        out = json.loads(r.read())
    if out.get("status") != "SUCCESS":
        # Never echo the payload back; it carries the keys.
        raise RuntimeError(f"{path} failed: {out.get('message', out)}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write the records")
    ap.add_argument("--proton-dkim", nargs=3, metavar="TARGET", default=(),
                    help="the three CNAME targets Proton shows for this domain")
    args = ap.parse_args()

    key, secret = creds()
    call("ping", key, secret)
    print(f"authenticated to Porkbun, domain {DOMAIN}")

    existing = call(f"dns/retrieve/{DOMAIN}", key, secret)["records"]
    have = {
        (r["type"], r["name"].removesuffix(f".{DOMAIN}").removesuffix(DOMAIN).rstrip("."),
         r["content"].rstrip("."))
        for r in existing
    }
    print(f"{len(existing)} record(s) already in the zone")

    want = records(tuple(args.proton_dkim))
    if not args.proton_dkim:
        print("\nNote: Proton's three DKIM targets are unique to your domain and only "
              "visible in Proton's setup screen. Re-run with --proton-dkim T1 T2 T3 to "
              "add them; everything else is included now.")

    todo = []
    for r in want:
        if (r["type"], r["name"], r["content"].rstrip(".")) in have:
            print(f"  ok, exists   {r['type']:5} {r['name'] or '@':32} {r['why']}")
        else:
            todo.append(r)
            print(f"  to add       {r['type']:5} {r['name'] or '@':32} {r['why']}")

    if not todo:
        print("\nnothing to add")
        return 0
    if not args.apply:
        print(f"\nDRY RUN: {len(todo)} record(s) would be added. Re-run with --apply.")
        return 0

    for r in todo:
        body = {"name": r["name"], "type": r["type"], "content": r["content"], "ttl": "600"}
        if r.get("prio"):
            body["prio"] = r["prio"]
        call(f"dns/create/{DOMAIN}", key, secret, **body)
        print(f"  added {r['type']} {r['name'] or '@'}")

    print(f"\nadded {len(todo)} record(s). Porkbun's TTL floor is 600s, so allow "
          "about ten minutes, then verify:")
    print(f"  dig +short MX {DOMAIN}")
    print(f"  dig +short TXT {DOMAIN}")
    print(f"  aws sesv2 get-email-identity --region us-east-1 "
          f"--email-identity {DOMAIN} --query "
          "'{dkim:DkimAttributes.Status,mailfrom:MailFromAttributes.MailFromDomainStatus}'")
    return 0


if __name__ == "__main__":
    sys.exit(main())
