# Moving the project to a dedicated AWS account

This project now runs in an AWS account of its own, a Control Tower account in
the organization's Workloads OU, reached through the `gnw` profile. It was
previously deployed somewhere it did not belong, because the ambient
`AWS_PROFILE` decided the target and nothing checked.

Account identifiers are deliberately not recorded here. Read them from the
environment:

    aws sts get-caller-identity --profile gnw

## Guards, so this cannot recur

- `infra/app.py` refuses to synth without `-c account=<id>` and refuses when the
  active credentials disagree with it. This fires on `cdk bootstrap` too, since
  bootstrap synths the app.
- `ops/send_notifications.py` requires `--account` and checks it against STS
  before building a single message. It earned this immediately: with a stale
  `AWS_PROFILE` still set, a dry run reached for the wrong account's credentials
  and was refused.
- Deploys therefore always name the target:

      export AWS_PROFILE=gnw
      cdk deploy GnwSiteStack     -c account=<id>
      cdk deploy GnwPipelineStack -c account=<id> -c alertEmail=<address>

## What the migration did

1. Disabled the monthly EventBridge schedule in the old account so nothing ran
   there again.
2. Deleted the project's SES footprint from the old account: domain identity,
   the `gnw-notify` configuration set, and its event destination. Left every
   unrelated resource in that account alone.
3. Created the new account through Control Tower Account Factory into the
   Workloads OU, reusing the existing Identity Center user so no second user was
   created.
4. Copied the data bucket to a staging bucket in the new account and verified all
   4,287 objects and 8,647,316,011 bytes matched by key and size before deleting
   anything.
5. Destroyed both stacks in the old account, then removed what CloudFormation
   left behind: two `RETAIN` buckets, the log group, and the container images in
   the CDK asset repository. The budget and the ACM certificate went with the
   stacks. Left the shared `CDKToolkit` stack in place.
6. Removed the stale records from DNS, then stood the new account up and pointed
   the domain at it.

## Traps hit, worth knowing before doing this again

- **Local data was not a usable substitute for the bucket.** 271 blobs were
  missing locally and all nine flags parquets differed, with the largest at
  3.95 GB in S3 against 3.81 GB on disk. Always copy S3 to S3 and verify by key
  and size; never assume the laptop has a complete copy.
- **Cross-account `s3 sync` needs `s3:GetObjectTagging`.** Without it only the
  small objects copy and every multipart object fails with `AccessDenied` on
  `GetObjectTagging`. 69 objects failed the first pass for exactly this.
- **CloudFront refuses an alias whose DNS points at another distribution.** The
  first deploy failed with a 409 because DNS still pointed at the old
  distribution's domain. The apex and `www` records have to come down *before*
  the new distribution is created, which is why the site goes dark during the
  swap. Do not attempt this after launch.
- **A rollback deletes the certificate but keeps a `RETAIN` bucket.** After the
  409, the retry failed with `gnw-site-<account> already exists`. Delete the
  empty bucket and let the stack mint a fresh certificate.
- **ACM validation record names are stable per domain per account.** The
  re-issued certificate asked for exactly the CNAMEs already in the zone, so it
  validated with no further DNS work.
- **The default-VPC assumption did not hold.** The stack used to look up the
  default VPC. A Control Tower account has none, and the VPC it does have had no
  internet gateway and no NAT, only an S3 endpoint. The crawl stage could never
  have reached an insurer's server from it, and the failure would have looked
  like a mysteriously empty run rather than an error. `GnwPipelineStack` now
  creates its own VPC with two public subnets and `nat_gateways=0`. Verified
  afterwards with a throwaway Fargate task that fetched three real directory
  URLs and a CMS endpoint from the new subnets.
- **Create the SES identity after the DNS records exist.** Done the other way
  round, DKIM sat `PENDING` for hours, because SES polls once on creation and the
  SOA negative-cache TTL then holds the failure. With the records already in
  place, DKIM and the custom MAIL FROM both verified almost immediately.
- **Cost-allocation tags are managed from the organization's management account**,
  not the member account, and activation is not retroactive.
- **Do not filter the budget on a project tag in a single-purpose account.** Every
  dollar in the account is already the project, and the filter silently drops the
  Control Tower baseline (Config, CloudTrail) plus the CDK bootstrap bucket and
  ECR repository, none of which carry the tag. Unfiltered is simpler and safer.

## Remaining

- **Watch the first full month of spend.** The $10 budget limit was set when the
  budget counted only tagged project resources. It now covers the whole account,
  including the Control Tower baseline. It should still come in under the limit,
  but the first month is the one to check before trusting the threshold.
- **Delete the staging bucket** once the first scheduled run proves the new setup.
  It holds a verified duplicate of the data plus a snapshot of the old site
  bucket, and costs a few cents a month to keep as a safety margin.
