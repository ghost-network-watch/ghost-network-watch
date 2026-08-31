#!/bin/bash
# One-time history rewrite, to run BEFORE the repo is ever made public.
#
# Removes from every commit:
#   - 13 MB of raw insurer directory dumps carrying real providers' names,
#     sex, languages, addresses and phone numbers. The derived exhibit files
#     next to them (alliant-TN-top3-records.json, pfpdata-...-top3-records.json,
#     medica-MO2-top5-records.json) and exhibit-address-inflation.json keep the
#     finding and its method, so nothing reproducible is lost.
#   - the full text of a paywalled Health Affairs article (Zhu 2022). The
#     citation stays in prior_art_citations.json.
#   - infra/cdk.context.json, which carries the AWS account id. Bucket names
#     are derived from that id (gnw-data-<account>), so publishing it names
#     the private evidence bucket. The file is a VPC lookup cache and
#     regenerates on the next cdk synth.
#   - .DS_Store.
# Also rewrites the two auto-derived laptop-hostname author emails to the
# GitHub noreply address, so commits attribute without publishing a real
# mailbox.
#
# A full backup of the current history is at
# ~/gnw-history-backup-2026-08-31.bundle (restore: git clone <bundle> <dir>).
set -euo pipefail
cd "$(dirname "$0")/.."

command -v git-filter-repo >/dev/null 2>&1 || PATH="$PWD/.venv/bin:$PATH"
export PATH

MAILMAP="$(mktemp)"
cat > "$MAILMAP" <<'EOF'
Soorena Sasani <911758+Soorena@users.noreply.github.com> <soorena@Soorenas-MacBook-Air.local>
Soorena Sasani <911758+Soorena@users.noreply.github.com> <soorena@mac.mynetworksettings.com>
EOF

git-filter-repo --force --mailmap "$MAILMAP" \
  --invert-paths \
  --path infra/cdk.context.json \
  --path .DS_Store \
  --path scoping/evidence/zhu-2022-phantom-networks-healthaffairs-fulltext.txt \
  --path scoping/evidence/address-inflation/fetch/alliant-ProvidersTN.head10mb.json \
  --path scoping/evidence/address-inflation/fetch/pfpdata-11269-wy-vs-provider.head10mb.json \
  --path scoping/evidence/address-inflation/fetch/pfpdata-11269-wy-vs-provider.tail3mb.json

rm -f "$MAILMAP"

echo
echo "=== verification ==="
for p in infra/cdk.context.json .DS_Store \
         scoping/evidence/zhu-2022-phantom-networks-healthaffairs-fulltext.txt \
         scoping/evidence/address-inflation/fetch/alliant-ProvidersTN.head10mb.json \
         scoping/evidence/address-inflation/fetch/pfpdata-11269-wy-vs-provider.head10mb.json \
         scoping/evidence/address-inflation/fetch/pfpdata-11269-wy-vs-provider.tail3mb.json; do
  n=$(git log --all --oneline -- "$p" | wc -l | tr -d ' ')
  printf '%s commits remain for %s\n' "$n" "$p"
done
echo
echo "Author emails now in history:"
git log --all --format='%ae' | sort -u
echo
echo "git-filter-repo drops the 'origin' remote on purpose. Because the old"
echo "objects stay reachable by direct SHA on GitHub even after a force push,"
echo "and this repo has never been public, the clean move is to delete the"
echo "GitHub repo and push fresh:"
echo
echo "  gh repo delete ghost-network-watch/ghost-network-watch --yes"
echo "  gh repo create ghost-network-watch/ghost-network-watch --private --source=. --remote=origin --push"
echo
echo "Verify the 6 paths are absent on GitHub, THEN flip to public."
