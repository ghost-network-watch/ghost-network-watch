# Deploying and tearing down Ghost Network Watch on AWS

Copy-paste runbook. Two stacks: the website (GnwSiteStack) and the monthly
pipeline (GnwPipelineStack). You can deploy just the website and keep running
the pipeline on the laptop; the pipeline stack is optional and separate.

Everything runs in us-east-1. Expected total cost is under 5 USD per month.

---

## 0. Prerequisites (one time)

1. AWS CLI logged in to your account: `aws sts get-caller-identity` shows it.
2. Node.js installed (CDK ships as an npm package; npx fetches it).
3. Python 3.11+ and Docker Desktop running (Docker is only needed for the
   pipeline stack, which builds a container image).

```bash
cd ~/ghost-network-watch/infra
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
source .venv/bin/activate
npx cdk bootstrap        # one time per AWS account; creates CDK's helper bucket
```

---

## 1. Deploy the website

```bash
npx cdk deploy GnwSiteStack
```

The deploy will PAUSE at certificate validation. While it waits:

1. Open the ACM console (us-east-1), click the pending certificate.
2. It shows two CNAME records (one for ghostnetworkwatch.org, one for www).
3. In Porkbun > ghostnetworkwatch.org > DNS, add both exactly as shown
   (type CNAME, host is the long _xxxx string WITHOUT the domain suffix,
   answer is the _yyyy.acm-validations.aws value).
4. Wait 5 to 30 minutes; the deploy resumes and finishes on its own.

When it finishes, note the three outputs: SiteBucketName, DistributionId,
DistributionDomain (looks like dxxxxxxxx.cloudfront.net).

### Point the domain at CloudFront (Porkbun)

| Type  | Host                      | Answer               |
|-------|---------------------------|----------------------|
| ALIAS | ghostnetworkwatch.org     | (DistributionDomain) |
| CNAME | www.ghostnetworkwatch.org | (DistributionDomain) |

Delete any Porkbun parking records on the apex first.

### Upload the site (first time, from the laptop)

```bash
cd ~/ghost-network-watch
.venv/bin/python -m gnw.cli site --snapshot 2026-08     # fresh build
aws s3 sync site/dist s3://SITE_BUCKET_NAME --delete
```

Check https://ghostnetworkwatch.org after DNS propagates (minutes to an hour).
Re-deploying the site later is the same two commands plus an invalidation:

```bash
aws cloudfront create-invalidation --distribution-id DISTRIBUTION_ID --paths "/*"
```

---

## 2. Deploy the monthly pipeline (optional, when leaving the laptop)

```bash
cd ~/ghost-network-watch/infra
npx cdk deploy GnwPipelineStack -c alertEmail=soorena@pm.me
```

1. Docker builds the image on first deploy; takes a few minutes.
2. SNS sends a confirmation email to the alert address. CLICK CONFIRM in it,
   or failure alerts will never arrive.
3. Note the DataBucketName output.

### Seed the data bucket (one time, from the laptop)

```bash
cd ~/ghost-network-watch
aws s3 sync data/blobs      s3://DATA_BUCKET_NAME/blobs
aws s3 sync data/reference  s3://DATA_BUCKET_NAME/reference
aws s3 sync ~/soorena.io/webawesome s3://DATA_BUCKET_NAME/webawesome-kit
```

### Verify with one manual run (recommended)

Use the ManualRunCommand stack output (fill in a public subnet id from the
VPC console), or just wait for the 15th. Watch it in the ECS console; logs are
in CloudWatch under /aws/ecs. A run takes 6 to 10 hours. Success means the
site updated and new files appeared under snapshots/ in the data bucket.

### What runs monthly, automatically

The 15th, 12:00 UTC: crawl, parse, reference data, flags, scores, site build,
site deploy, notification bundles (generated into the data bucket, never
sent). You get an email only on failure. Your monthly touchpoints: skim the
new snapshot, decide about sending issuer notifications, answer corrections.

### Yearly maintenance (each fall)

Replace scoping/data/mr-puf-2026.csv with the new plan year's Machine-Readable
URL PUF and update the reference PUF URLs in pipeline/gnw/reference.py, then
redeploy the pipeline stack so the image picks it up.

---

## 3. Pause without tearing down

- Stop the monthly runs: EventBridge console > Rules > the rule named like
  GnwPipelineStack-MonthlySchedule > Disable. (Or redeploy with
  `-c scheduleEnabled=false`.) Cost drops to S3 storage only, about 1 USD.
- The website has no schedule and nothing to pause; it costs pennies at rest.

---

## 4. Tear down completely

Order matters. Buckets are RETAINED by CloudFormation on purpose (so a stack
mistake can never delete the evidence archive), which means you empty and
delete them yourself.

### 4a. Pipeline stack

```bash
# 1. Disable the schedule first (see section 3), so nothing launches mid-teardown.
# 2. Destroy the stack (removes cluster, task, VPC, schedule, alerts):
cd ~/ghost-network-watch/infra && npx cdk destroy GnwPipelineStack

# 3. The data bucket survives. If you truly want it gone, back it up first:
aws s3 sync s3://DATA_BUCKET_NAME ~/gnw-data-backup
aws s3 rm s3://DATA_BUCKET_NAME --recursive
aws s3 rb s3://DATA_BUCKET_NAME
```

### 4b. Site stack

```bash
# 1. Point Porkbun DNS away first (or delete the ALIAS and CNAME records),
#    so the domain never serves a deleted distribution.
npx cdk destroy GnwSiteStack

# 2. The site bucket survives; empty and delete it if wanted:
aws s3 rm s3://SITE_BUCKET_NAME --recursive
aws s3 rb s3://SITE_BUCKET_NAME
```

### 4c. Leftovers checklist (all optional, all small)

- ACM certificate: deleted with the stack; if it lingers, delete in ACM console.
- ECR: the pipeline image lives in the CDK assets repository; delete old
  images in ECR console if you want zero storage.
- CloudWatch log groups /aws/ecs/... : delete in console (pennies otherwise).
- CDK bootstrap stack (CDKToolkit) and its bucket: only remove if you use CDK
  for nothing else in this account.
- Porkbun: the two ACM validation CNAMEs can be deleted any time after teardown.

### 4d. Confirm the bill is zero

Billing console > Bills > filter by service. After teardown the only possible
lines are S3 (if you kept buckets) and Route 53 (not used; DNS is at Porkbun).

---

## Quick reference

| Task                    | Command                                                        |
|-------------------------|----------------------------------------------------------------|
| Monthly run on laptop   | `ops/run_monthly.sh`                                           |
| Deploy site changes     | `gnw site` then `aws s3 sync site/dist s3://SITE_BUCKET --delete` + invalidation |
| Pause cloud pipeline    | disable EventBridge rule MonthlySchedule                       |
| Resume                  | enable the same rule                                           |
| Manual cloud run        | ManualRunCommand stack output                                  |
| Destroy pipeline        | disable rule, `cdk destroy GnwPipelineStack`, empty data bucket |
| Destroy site            | repoint DNS, `cdk destroy GnwSiteStack`, empty site bucket     |
