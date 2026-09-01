# SES production access request

**Submitted 2026-09-01** from the project's own account, `ReviewDetails.Status:
PENDING`. Nothing to do here unless it is denied. Check with:

    aws sesv2 get-account --profile gnw --region us-east-1 \
      --query '{prod:ProductionAccessEnabled,review:Details.ReviewDetails}'

`ProductionAccessEnabled: true` and a 24-hour quota above 200 mean it was
granted. If it comes back `DENIED`, the reason lands in `ReviewDetails.CaseId`
and the reply goes to `soorena@proton.me`; answer the case rather than
resubmitting, since a second blind request invites a second denial.

The account is in the SES sandbox, which only delivers to separately verified
addresses. The issuer pre-notification send needs production access. Review the
description below, then submit it.

Reviewers care about four things: where the addresses came from, whether the
recipients expect the mail, how bounces and complaints are handled, and volume.
The description answers all four in that order.

## Submit

```bash
aws sesv2 put-account-details \
  --region us-east-1 \
  --mail-type TRANSACTIONAL \
  --website-url https://ghostnetworkwatch.org \
  --contact-language EN \
  --additional-contact-email-addresses soorena@proton.me \
  --production-access-enabled \
  --use-case-description "$(cat ops/ses_use_case.txt)"
```

Turnaround is typically about one business day. Check status with:

```bash
aws sesv2 get-account --region us-east-1 \
  --query '{prod:ProductionAccessEnabled,quota:SendQuota}'
```

## Before submitting

- **Check the account first.** `aws sts get-caller-identity`. This request must
  come from the account that owns the verified domain. Production access is
  granted per account, so submitting from the wrong one buys nothing and has to
  be redone. The stack was once deployed to the wrong account entirely; see
  `ops/migrate_account.md`.
- The DKIM CNAMEs and the bounce-subdomain MX and SPF records should already be
  in DNS. A request from a domain that is not yet verified invites a denial.
  After the account migration the DKIM tokens are new; the old ones are dead.
- `ops/send_notifications.py --account <id> --snapshot <s>` (no `--send`) should
  run clean, so the answer to "what will you send" is something you have actually
  seen. Verified clean on the 2026-08 bundles: 185 messages, 90.0 MB, all under
  the per-message limit.

## After approval

Deploy the pipeline stack once more if you have not since the SNS policy change,
so SES can publish bounce and complaint events to the alerts topic. Without it a
bounce is invisible, and a bounce means an issuer was never notified and its
14-day review window never started.
