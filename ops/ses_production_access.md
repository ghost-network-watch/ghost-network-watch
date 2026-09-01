# SES production access request

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

- The DKIM CNAMEs and the bounce-subdomain MX and SPF records should already be
  in DNS. A request from a domain that is not yet verified invites a denial.
- `ops/send_notifications.py --snapshot <s>` (no `--send`) should run clean, so
  the answer to "what will you send" is something you have actually seen.

## After approval

Deploy the pipeline stack once more if you have not since the SNS policy change,
so SES can publish bounce and complaint events to the alerts topic. Without it a
bounce is invisible, and a bounce means an issuer was never notified and its
14-day review window never started.
