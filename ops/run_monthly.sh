#!/bin/bash
# One monthly run, end to end. Works two ways:
#   Laptop:  ops/run_monthly.sh            (uses local data/, no cloud needed)
#   Cloud:   set GNW_DATA_BUCKET (and optionally GNW_SITE_BUCKET,
#            GNW_DISTRIBUTION_ID) and the same script syncs state down from S3,
#            runs, syncs results up, and deploys the site.
# Issuer notification emails are NEVER sent by this script. It generates the
# bundles; sending is a deliberate human step.
set -euo pipefail

cd "$(dirname "$0")/.."
SNAPSHOT="${GNW_SNAPSHOT:-$(date -u +%Y-%m)}"
PUBLISH_DATE="${GNW_PUBLISH_DATE:-$(date -u -d '+21 days' +%Y-%m-%d 2>/dev/null || date -u -v+21d +%Y-%m-%d)}"
FETCH_DATE="$(date -u +%Y-%m-%d)"
PY="${GNW_PYTHON:-.venv/bin/python}"
command -v "$PY" >/dev/null 2>&1 || PY=python3

echo "=== GNW monthly run: snapshot $SNAPSHOT (fetch $FETCH_DATE) ==="

if [ -n "${GNW_DATA_BUCKET:-}" ]; then
  echo "--- sync state down from s3://$GNW_DATA_BUCKET"
  mkdir -p data
  # Blobs enable content dedup across months; reference gets refreshed anyway.
  aws s3 sync "s3://$GNW_DATA_BUCKET/blobs" data/blobs --only-show-errors
  aws s3 sync "s3://$GNW_DATA_BUCKET/reference" data/reference --only-show-errors
  # The Web Awesome kit is licensed and lives in the private data bucket,
  # never in the public repo or image.
  aws s3 sync "s3://$GNW_DATA_BUCKET/webawesome-kit" data/webawesome-kit --only-show-errors
  export GNW_WA_KIT="data/webawesome-kit"
fi

echo "--- crawl"
$PY -m gnw.cli crawl --snapshot "$SNAPSHOT" --workers 8
echo "--- parse"
$PY -m gnw.cli parse --snapshot "$SNAPSHOT"
echo "--- reference data"
$PY -m gnw.cli refs --source all
echo "--- compact join tables"
$PY -m gnw.cli compact --snapshot "$SNAPSHOT"
echo "--- flags"
$PY -m gnw.cli flags --snapshot "$SNAPSHOT" --fetch-date "$FETCH_DATE"
echo "--- scores"
$PY -m gnw.cli score --snapshot "$SNAPSHOT"
echo "--- site"
$PY -m gnw.cli site --snapshot "$SNAPSHOT" ${GNW_WA_KIT:+--wa-kit "$GNW_WA_KIT"}
echo "--- notification bundles (generated, not sent)"
$PY -m gnw.cli notify --snapshot "$SNAPSHOT" --publish-date "$PUBLISH_DATE"

if [ -n "${GNW_DATA_BUCKET:-}" ]; then
  echo "--- sync results up to s3://$GNW_DATA_BUCKET"
  aws s3 sync data/blobs "s3://$GNW_DATA_BUCKET/blobs" --only-show-errors
  aws s3 sync data/reference "s3://$GNW_DATA_BUCKET/reference" --only-show-errors
  for prefix in snapshots scores flags notify; do
    aws s3 sync "data/$prefix/$SNAPSHOT" "s3://$GNW_DATA_BUCKET/$prefix/$SNAPSHOT" --only-show-errors || true
  done
  aws s3 sync "data/snapshots/$SNAPSHOT" "s3://$GNW_DATA_BUCKET/snapshots/$SNAPSHOT" --only-show-errors
fi

if [ -n "${GNW_SITE_BUCKET:-}" ]; then
  echo "--- deploy site to s3://$GNW_SITE_BUCKET"
  aws s3 sync site/dist "s3://$GNW_SITE_BUCKET" --delete --only-show-errors
  if [ -n "${GNW_DISTRIBUTION_ID:-}" ]; then
    aws cloudfront create-invalidation --distribution-id "$GNW_DISTRIBUTION_ID" \
      --paths "/*" >/dev/null
    echo "--- CloudFront invalidated"
  fi
fi

echo "=== done: snapshot $SNAPSHOT ==="
echo "Manual next steps: review scores, then send notification bundles when ready."
