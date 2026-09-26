# Canonical payment core — bounded delivery slice (historical PostgreSQL baseline)

> Status: superseded as the active delivery runbook by the [DynamoDB pilot storage decision](2026-09-13-dynamodb-pilot-storage.md) and [pilot deployment configuration](2026-09-13-pilot-deploy-configuration.md). This document is retained as historical design and qualification evidence; do not use its PostgreSQL or migration instructions for the pilot.

This delivery slice implements the smallest persistence and domain boundary from
[the active architecture](../../ARCHITECTURE.md). It does not execute the
superseded September 9 plan and does not alter the SUNWAVE application.

## Included

- PostgreSQL records for businesses, memberships, customers, secure charge
  links, charges, payment attempts, payments, allocations, provider events,
  financial adjustments, and outbox work.
- Server-controlled integer-minor-unit charge amounts and derived balances.
- Idempotent checkout submission, persisted attempt identity, and recovery of
  an uncertain Mercado Pago preference by its attempt reference using the
  provider's current `elements` search response.
- A Mercado Pago Checkout Pro boundary with one generic quantity-one item,
  seller/environment/amount/currency/reference validation, authoritative
  payment retrieval, current `init_point` buyer redirects, provider-confirmed
  `test_user` identity, and refund/chargeback normalization.
- Transactional retention of duplicate real payments, review flags for invalid
  provider facts or extra money, and adjustment-based balance reversal.
- Durable, idempotent full and partial refund requests. A leased worker uses the
  same provider idempotency key on every retry, then verifies the payment again
  before reflecting a refund or reversal in the charge balance. Only one
  unresolved refund may reserve a payment at a time.
- `GET /pay/{token}` for the minimal customer charge/status projection and
  accessible HTML status page, plus `POST /pay/{token}/attempts` with a required
  idempotency key and no buyer amount or description fields.
- JWT-protected staff routes for idempotent customer/charge creation, charge
  listing and cancellation, owner-authorized refunds, and review-queue access.
- A connection-scoped Mercado Pago webhook route that uses the official SDK's
  signature validator, durably captures valid evidence before authoritative
  retrieval, returns `202` only after persistence, quarantines mismatched
  resources, and leaves failures retryable.
- Leased provider-event, reconciliation, refund, and transactional-email outbox
  workers. Scheduled reconciliation searches every page and retains each real
  payment independently, including duplicates and out-of-order updates.
- Provider timestamps order the complete financial snapshot, so stale responses
  cannot change allocations, adjustments, review state, notifications, or audit
  facts. Refund and later chargeback effects are both retained without exceeding
  the original payment amount.
- When an adjustment changes the outstanding balance, the old Checkout Pro
  preference enters a durable expiration state. Mercado Pago must acknowledge
  its expiration before a new preference is created for the current balance.
- Owner-only review actions can retry authoritative processing or acknowledge an
  investigated item. Each action has an idempotency key, required note, durable
  resolution record, and audit event. Acknowledgement never creates an allocation.
- A scheduled aggregate health sentinel and CloudWatch alarms for worker errors,
  stalled durable work, or unresolved financial review items.
- Secrets Manager references for the database, access token, and webhook
  signature. Secret values are neither stored in the database nor logged.

## Not included

Deployment, managed PostgreSQL, backup/restore configuration, identity-provider
provisioning, a staff UI, merchant OAuth, and production credential execution
remain separate follow-up work. A browser return only re-reads the status
endpoint; it never confirms payment.

## Verification target

Unit tests cover secure links, duplicate submissions, uncertain provider
responses, provider validation, redirect non-authority, extra payments, webhook
durability, refund idempotency, staff authorization, and stalled-work detection.
PostgreSQL integration tests exercise concurrent checkout, lease recovery,
out-of-order observations, tenant-scoped idempotency, approval, duplicate
delivery, extra-payment retention, refund balance restoration, and cross-business
foreign-key rejection.

## HTTP configuration contract

The SAM template requires a database secret ARN, a tightly scoped provider-secret
ARN pattern, a customer status-page base URL, and the public API base URL. The
database secret contains `url`; merchant credential secrets contain
`access_token`; webhook secrets contain `webhook_secret`. Only sandbox connections
whose credential source is recorded as `test_credentials` are accepted by this
slice.

## Pilot operating runbook

Before any deployment, the operator must provide separate sandbox-only values
for the database secret, Mercado Pago credential and webhook secret, application
HMAC secret, JWT issuer/audience, exact staff origin, verified SES sender, and an
SNS alarm topic. Never reuse a live credential in the sandbox stack.

Apply migrations `001`, `002`, `003`, and `004` in order to a backed-up database. Prove
restore to a separate database before accepting money. Deploy to a sandbox stack,
confirm all four workers and the health sentinel are scheduled, and confirm an
alarm reaches an operator. Do not deploy while any health alarm or review item is
open.

For each release, qualify these cases with Mercado Pago test users: checkout
resume after interruption, concurrent duplicate clicks, approved payment,
pending payment, rejected payment, duplicate and out-of-order webhook delivery,
scheduled recovery when the webhook is absent, a second real payment, full and
partial refund, and reversal/chargeback observation. Verify the database record,
customer page, staff charge view, review queue, and outstanding balance after
each case. A redirect result is never acceptable evidence.

If provider submission has an uncertain outcome, keep the operation in its
durable retry state and reconcile by the stored attempt or refund idempotency key;
do not create a replacement operation. If an amount, account, environment, or
reference mismatch appears, leave funds recorded, do not allocate them, and
resolve the review item manually. If a worker alarm fires, stop new pilot charge
creation until the durable queues are moving again.

The local sandbox successfully created and verified Checkout Pro payments. A
dedicated application using credentials copied from its **Test credentials**
page was verified against Mercado Pago's identity API as a `test_user` seller.
During qualification, an approved MXN 10 test-card payment arrived while the
local webhook origin was unavailable; scheduled reconciliation recovered it
from Mercado Pago, and a later signed duplicate webhook produced neither a
second provider-event row nor a second allocation.

The active user explicitly authorized an operational policy update after two
separate MXN 10 Checkout Pro payments, each completed by the Mexico Buyer Test
User with an official test card, were returned as `live_mode=true`. The adapter
now treats `live_mode` as retained provider evidence rather than a sandbox
classifier: a payment read through a credential verified as the configured
`test_user` seller remains test-scoped. Allocation is still gated by the
persisted test connection, exact attempt reference, seller account, amount,
currency, and provider-approved state. A non-sandbox adapter or any other
connection mismatch remains review-only.

The second buyer-test qualification payment was consequently reconciled once
and allocated once for MXN 10; its stale environment review was cleared by the
newer valid observation. A durable full-refund operation was then submitted
with its stable provider idempotency key. Mercado Pago returned HTTP 401 from
the payment-refund endpoint, so that operation is in the review queue and no
refund adjustment or local balance reversal was simulated. Partial-refund
qualification remains blocked by that external provider response. Retry the
same stored operation after Mercado Pago resolves access to test-payment
refunds; do not create a replacement operation.
