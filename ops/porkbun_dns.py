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

# SES DKIM tokens for this domain, from the identity in us-east-1. Tokens are
# minted per identity per account, so moving the project to a different AWS
# account invalidated the previous set; those CNAMEs were deleted from the zone
# on 2026-09-01. If the account ever changes again, replace these and remove the
# stale records with --delete.
SES_DKIM = (
    "5nsfuvdwegcq7qqjhlytwlxhjrlqdnhe",
    "ahnkjvkqgsnpqqo2mcoeduph6m2ja2tx",
    "zua4pciepmyu5znnnsopmaqoiunkhqts",
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


#: Checked in order. Both a JSON file and a KEY=value file work, and the JSON
#: key names are matched loosely, because Porkbun's own docs use apikey and
#: secretapikey while everyone's notes-to-self use something else.
CRED_PATHS = ("~/.porkbun-creds.json", "~/.porkbun.env", "~/.config/porkbun.json")


def _from_json(text: str) -> tuple[str, str]:
    data = json.loads(text)
    flat: dict[str, str] = {}
    for k, v in data.items():
        if isinstance(v, dict):
            for k2, v2 in v.items():
                if isinstance(v2, str):
                    flat[f"{k}.{k2}".lower()] = v2
        elif isinstance(v, str):
            flat[k.lower()] = v
    secret = next((v for k, v in flat.items() if "secret" in k), "")
    key = next(
        (v for k, v in flat.items()
         if "secret" not in k and ("api" in k or "key" in k)),
        "",
    )
    return key, secret


def _from_env_file(text: str) -> tuple[str, str]:
    key = secret = ""
    for line in text.splitlines():
        line = line.strip()
        if "=" not in line or line.startswith("#"):
            continue
        name, _, value = line.partition("=")
        name, value = name.strip().lower(), value.strip().strip("'\"")
        if "secret" in name:
            secret = value
        elif "api" in name or "key" in name:
            key = value
    return key, secret


def creds() -> tuple[str, str]:
    key = os.environ.get("PORKBUN_API_KEY", "")
    secret = os.environ.get("PORKBUN_SECRET_KEY", "")
    source = "environment"
    if not (key and secret):
        candidates = []
        if os.environ.get("PORKBUN_CREDS"):
            candidates.append(os.environ["PORKBUN_CREDS"])
        candidates.extend(CRED_PATHS)
        for raw in candidates:
            path = Path(raw).expanduser()
            if not path.exists():
                continue
            text = path.read_text()
            try:
                key, secret = (
                    _from_json(text) if text.lstrip().startswith("{")
                    else _from_env_file(text)
                )
            except (json.JSONDecodeError, ValueError):
                continue
            if key and secret:
                source = str(path)
                break
    if not (key and secret):
        sys.exit(
            "No credentials found. Looked at $PORKBUN_API_KEY/$PORKBUN_SECRET_KEY and "
            + ", ".join(CRED_PATHS)
            + ".\nEither point PORKBUN_CREDS at your file or create ~/.porkbun.env with "
            "PORKBUN_API_KEY= and PORKBUN_SECRET_KEY=, then chmod 600 it."
        )
    # Say where they came from, never what they are.
    print(f"credentials loaded from {source}")
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
    ap.add_argument("--add", nargs=3, action="append", default=[],
                    metavar=("TYPE", "HOST", "CONTENT"),
                    help="add an arbitrary record, repeatable. HOST may be given "
                         "fully qualified with a trailing dot, as ACM and other "
                         "AWS services print it; the domain suffix is stripped "
                         "for you. Use @ or '' for the apex.")
    ap.add_argument("--list", action="store_true", help="print the zone and exit")
    ap.add_argument("--delete", nargs=2, action="append", default=[],
                    metavar=("TYPE", "HOST"),
                    help="delete every record matching TYPE and HOST, repeatable. "
                         "Refuses to touch anything Proton depends on unless "
                         "--force-proton is given.")
    ap.add_argument("--force-proton", action="store_true",
                    help="permit deleting a Proton record. Almost never right: the "
                         "verification TXT has to stay in place permanently.")
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

    def short(r):
        h = r["name"].removesuffix(f".{DOMAIN}").removesuffix(DOMAIN).rstrip(".")
        return f"  {r['type']:6} {h or '@':44} {r['content'][:60]}"

    if args.list:
        for r in sorted(existing, key=lambda r: (r["type"], r["name"])):
            print(short(r))
        return 0

    if args.delete:
        # Proton's records are load-bearing and one of them (the verification
        # TXT) has to persist forever, so they are excluded by default.
        doomed = []
        for rtype, host in args.delete:
            host = host.rstrip(".")
            if host.endswith(DOMAIN):
                host = host[: -len(DOMAIN)].rstrip(".")
            host = "" if host in ("@", "") else host
            for r in existing:
                rhost = r["name"].removesuffix(f".{DOMAIN}").removesuffix(DOMAIN).rstrip(".")
                if r["type"] != rtype.upper() or rhost != host:
                    continue
                if "proton" in r["content"].lower() and not args.force_proton:
                    print(f"  SKIP, Proton record{short(r)}")
                    continue
                doomed.append(r)
        if not doomed:
            print("\nnothing matched")
            return 0
        for r in doomed:
            print(f"  to delete{short(r)}")
        if not args.apply:
            print(f"\nDRY RUN: {len(doomed)} record(s) would be deleted. Re-run with --apply.")
            return 0
        for r in doomed:
            call(f"dns/delete/{DOMAIN}/{r['id']}", key, secret)
            print(f"  deleted {r['type']} {r['name']}")
        print(f"\ndeleted {len(doomed)} record(s)")
        return 0

    want = records(tuple(args.proton_dkim)) if not args.add else []
    for rtype, host, content in args.add:
        host = host.rstrip(".")
        if host.endswith(DOMAIN):
            host = host[: -len(DOMAIN)].rstrip(".")
        want.append({
            "type": rtype.upper(),
            "name": "" if host in ("@", "") else host,
            "content": content.rstrip("."),
            "why": "ad-hoc, from --add",
        })
    if not args.add and not args.proton_dkim:
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
