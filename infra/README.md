# Ghost Network Watch infrastructure

CDK app with two stacks. Nothing here is deployed automatically; deploy when
ready to move the monthly run off the laptop.

## What you get

- **GnwSiteStack**: private S3 bucket + CloudFront + ACM certificate for
  ghostnetworkwatch.org, directory-index rewriting at the edge, 404 page wired.
- **GnwPipelineStack**: a monthly Fargate task (4 vCPU, 16 GB, 200 GB disk,
  ARM64) that runs ops/run_monthly.sh end to end, a data bucket with a
  Glacier lifecycle for the evidence archive, and an email alert on failure.

## Monthly cadence after deploy

You run nothing. EventBridge starts the task on the 15th at 12:00 UTC, after
the federal registry's monthly release. The task crawls, parses, flags,
scores, rebuilds the site, deploys it, and generates the issuer notification
bundles into the data bucket. Sending notifications stays a human step, on
purpose. If the task fails you get an email; otherwise the site just updates.

Your actual monthly touchpoints:
1. Skim the new snapshot (grades, league, anything surprising).
2. Send or skip the issuer notification emails for new findings.
3. Handle any correction requests.

## Deploy

    cd infra
    python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
    npx cdk bootstrap                      # once per account
    npx cdk deploy GnwSiteStack
    # Add the ACM DNS validation CNAMEs to Porkbun when the deploy pauses.
    # Then point Porkbun at the distribution:
    #   ALIAS  ghostnetworkwatch.org     -> <DistributionDomain output>
    #   CNAME  www.ghostnetworkwatch.org -> <DistributionDomain output>
    npx cdk deploy GnwPipelineStack -c alertEmail=you@example.com

One-time seeding of the data bucket from the laptop:

    aws s3 sync data/blobs      s3://gnw-data-<account>/blobs
    aws s3 sync data/reference  s3://gnw-data-<account>/reference
    aws s3 sync ~/soorena.io/webawesome s3://gnw-data-<account>/webawesome-kit
    aws s3 sync site/dist       s3://gnw-site-<account> --delete

The Web Awesome kit is licensed; it lives in the private data bucket and the
private image, never in the public repo.

## Cost envelope

Fargate ARM64 4 vCPU 16 GB for a 6 to 10 hour monthly run: about 1.50 to 2.50
USD per month. S3 data bucket: pennies now, roughly 1 USD per month after a
year of snapshots with the Glacier lifecycle. CloudFront stays inside the
permanent free tier at any realistic traffic. Site bucket storage: under
0.01 USD. Total well under 5 USD per month.

## Kill switch and manual runs

Disable the schedule: EventBridge rule "MonthlySchedule" in the console, or
redeploy with -c scheduleEnabled=false. Run outside the schedule with the
ManualRunCommand stack output. Run locally exactly as before:
ops/run_monthly.sh with no environment variables.
