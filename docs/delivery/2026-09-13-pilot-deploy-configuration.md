# Pilot deploy configuration and runbook

Status: active runbook, last verified 2026-09-14 against `codex/canonical-payment-slice`
through the fifth pilot-readiness-audit remediation pass
(see `docs/delivery/2026-09-13-pilot-readiness-audit.md`). Its operational content
(secrets, OIDC role, environment variables, why this is not a GitHub
`environment`) was unaffected by that pass's backend/IAM/docs changes; only the
verification date and pass reference are updated here. This document is the
operational authority for the manual pilot deploy; `docs/ARCHITECTURE.md`
remains the product/architecture authority.

The manual GitHub workflow is `.github/workflows/deploy-pilot.yml`. It only
runs on `workflow_dispatch` and it must remain that way for the pilot: no
`on: push` trigger exists, and none should be added while this is a sandbox
pilot.

## Why this is not a GitHub `environment`

The workflow was originally written against a protected environment named
`pilot` (`environment: pilot` in the job, with environment-scoped secrets).
That does not fit this repository's constraints: it is a **private**
repository on a **personal (Free) GitHub account**, and environment
protection rules (required reviewers, wait timers, deployment-branch
restrictions) are a paid-plan feature for private repositories. Depending on
protection rules that silently do not apply — or on an environment GitHub
lets you create but not actually protect — would be worse than not using one.

The workflow now reads **repository-level** secrets and variables instead.
Mutual exclusion of concurrent deploys comes from the `concurrency:` block
keyed on the stack name, not from an environment. Nothing below requires
GitHub Pro/Team, and the repository stays private.

## Repository secrets and variables to create

In the private repo's **Settings → Secrets and variables → Actions**, at the
**repository** level (not an environment):

| Kind | Name | Value |
| --- | --- | --- |
| Variable | `AWS_REGION` | Target AWS region, initially `us-east-1`. |
| Secret | `AWS_DEPLOY_ROLE_ARN` | The GitHub OIDC deployment role ARN, see trust policy below. |
| Secret | `APPLICATION_SECRET_ARN` | ARN of the secret containing `link_token_hmac`. |
| Secret | `PROVIDER_SECRETS_ARN_PATTERN` | Scoped ARN/pattern for the sandbox merchant credential and webhook secrets. |

The staff origin, Cognito/OIDC issuer and audience, SES sender, and alert-topic
ARN are supplied as `workflow_dispatch` inputs at dispatch time, not stored as
secrets — they are deployment configuration, not source-code tokens, and they
change less predictably than a fixed value would allow. The customer payment
link and the webhook URL are never supplied by a human: the public and staff
Lambda handlers derive them from API Gateway's own trusted request metadata
(`requestContext.domainName`/`stage`), so nobody needs to know the API's URL
before the first deploy. See "Behavior if a custom domain or proxy is added
later" below for what changes if that stops being true.

## AWS OIDC trust policy (external — not created by this audit)

`AWS_DEPLOY_ROLE_ARN` must be an IAM role with a trust policy restricted to
this exact GitHub repository and the exact ref this workflow deploys from. Do
not use a wildcard `sub` condition. Once PR #2 is merged (see "First deploy
order" below), the condition should read exactly:

```json
{
  "Effect": "Allow",
  "Principal": {"Federated": "arn:aws:iam::<account-id>:oidc-provider/token.actions.githubusercontent.com"},
  "Action": "sts:AssumeRoleWithWebIdentity",
  "Condition": {
    "StringEquals": {
      "token.actions.githubusercontent.com:aud": "sts.amazonaws.com",
      "token.actions.githubusercontent.com:sub": "repo:jorgecantu276/servicios-financieros-beta:ref:refs/heads/main"
    }
  }
}
```

Deploying only from `main` (rather than `repo:...:*` or the feature branch)
means the pilot can only ever deploy reviewed, merged code. This audit does
not create the OIDC identity provider, the role, or its permissions policy —
that is a real AWS change, out of scope here, and must be done deliberately
by whoever owns the AWS account.

## Preconditions for the first run

1. A staff identity provider exists and its issuer/audience are known. Cognito
   is the intended pilot choice, but user lifecycle and the actual callback
   URLs must be chosen before creating users.
2. The staff app has an HTTPS origin. The API accepts only that exact origin
   for CORS; it never uses a wildcard.
3. The two referenced Secrets Manager secrets exist, contain sandbox
   credentials only, and the merchant verification command has succeeded
   after tenant onboarding.
4. An SNS topic and a verified SES sender exist. The alert topic must have a
   human subscriber before a pilot payment is accepted — an alarm nobody
   receives is not a working alarm.
5. The AWS OIDC role above exists with the trust policy above.

## What the workflow creates

The SAM stack creates the API Gateway HTTP API, Lambda functions and
schedules, an on-demand, encrypted DynamoDB table with point-in-time recovery
(`DeletionPolicy: Retain` / `UpdateReplacePolicy: Retain`), and CloudWatch
alarms. It does **not** create a database instance, VPC, NAT Gateway, Cognito
tenant, frontend hosting, a merchant connection, or WhatsApp delivery. Those
remain external preconditions or later work, not something this workflow
fakes into existing.

## First deploy order

This is the actual order, not an arbitrary preference — steps 1 and 2 are
hard GitHub constraints, not this team's convention:

1. **Merge PR #2 to `main`.** GitHub only ever triggers `workflow_dispatch`
   for a workflow file that exists on the repository's default branch — this
   is a platform rule, not a repository setting, and it applies regardless of
   which ref you ask the dispatch to run against. Today `.github/workflows/`
   only exists on `codex/canonical-payment-slice`; `main` is still the
   pre-existing skeleton. Until the merge, `deploy-pilot.yml` (and `verify.yml`)
   cannot be dispatched at all, from the UI or the API/`gh` CLI. This audit
   does not merge the PR.
2. Create the repository secrets/variables above, and the AWS OIDC role with
   its trust policy scoped to `ref:refs/heads/main`.
3. Confirm the staff identity provider, HTTPS staff origin, SES sender, and
   SNS topic (with a human subscriber) all exist — the preconditions above.
4. From the Actions tab on `main`, run **Deploy pilot** via `workflow_dispatch`
   with the required inputs (stack name, staff origin, staff auth
   issuer/audience, SES sender, alarm topic ARN).
5. Read the `ApiUrl` stack output (or the workflow's own "Stack outputs" step)
   and run the smoke tests below against it.

## Smoke tests after a deploy

Run these against the `ApiUrl` output; none of them require a merchant
connection or a real card:

1. `GET {ApiUrl}/health` returns `200 {"ok": true}`.
2. `GET {ApiUrl}/pay/{any-16+-char-token}` returns `404 {"error":"charge_not_found"}`
   — confirms the public route is live and does not leak a stack trace for an
   unknown link.
3. A staff call without an `Authorization` header against
   `{ApiUrl}/businesses/{id}/customers` returns `401`, not `500` — confirms the
   JWT authorizer is actually wired to `StaffAuthIssuer`/`StaffAuthAudience`.
4. Once a real staff JWT and a real business/customer exist (needs the
   identity provider and manual onboarding — not part of this workflow),
   create a charge through the staff API and confirm the returned
   `paymentUrl` is `{ApiUrl}/pay/{token}` with `/v1` appearing exactly once,
   and that opening it renders the public payment page.
5. `GET {ApiUrl}/businesses/{id}/summary` with that same staff JWT returns
   `{"openChargeCount": N, "outstandingMinor": M}` matching what the staff
   workspace's "Hoy" screen shows for "Pendientes"/"Saldo por cobrar" — this
   is the uncapped aggregate added during the pilot readiness audit; it must
   never come back lower than a manual count for a business with more than
   100 open charges.
6. Confirm the CloudWatch alarms exist in the stack (`aws cloudformation
   describe-stacks`) and that the SNS topic ARN passed at dispatch matches a
   topic with a confirmed human subscriber — an alarm with no subscriber is
   silent by design and must not be treated as monitoring. Note the alarms
   only fire on Lambda invocation *errors*; they do not detect a worker that
   runs successfully but silently claims zero work (see the pilot readiness
   audit, finding P2) — watch actual outbox/refund/reconciliation throughput
   during the pilot, not just these alarms.

None of the above sends a real payment; qualifying an actual Mercado Pago
sandbox checkout end-to-end is a separate, manual step against the deployed
stack and is not scripted by this workflow.

## Recovery procedure

- **Failed deploy (template/parameter error):** `sam deploy` fails before
  changing the running stack if the changeset is invalid; re-run after fixing
  the input. `--no-fail-on-empty-changeset` only avoids a false failure when
  nothing changed — it does not skip real validation errors.
- **Stack stuck in `UPDATE_ROLLBACK_FAILED` or similar:** this requires an AWS
  console/CLI action (e.g. `aws cloudformation continue-update-rollback`) by
  whoever holds AWS access; this audit does not perform it.
- **Wrong stack deployed to the wrong region/account:** the workflow always
  assumes a role scoped by the OIDC trust policy to one repository/ref: it
  cannot deploy anywhere the role's own permissions boundary does not already
  allow. Fix the role/permissions policy, not the workflow, if this happens.
- **Need to redeploy after a code fix:** merge the fix to `main`, re-run
  `workflow_dispatch` with the same `stack_name`. The `concurrency:` group
  keyed on `stack_name` queues a second dispatch rather than racing it; it
  does not cancel an in-flight deploy (`cancel-in-progress: false`), so two
  dispatches to the same stack run one after the other, never concurrently.
- **DynamoDB table needs restoring:** PITR is enabled
  (`PointInTimeRecoverySpecification.PointInTimeRecoveryEnabled: true`) and
  the table has `DeletionPolicy`/`UpdateReplacePolicy: Retain`, so deleting or
  replacing the stack does not delete point-in-time recovery data or the table
  itself; restoring from PITR is a separate, manual AWS operation this audit
  does not perform or rehearse.

## Behavior if a custom domain or proxy is added later

Today the public payment link, the staff-generated `paymentUrl`, the Mercado
Pago return URL, and the webhook `notification_url` are all derived from the
same trusted API Gateway request metadata
(`backend/src/payments/api_gateway.py:request_base_url`), because everything
is served by one API Gateway HTTP API with no domain or CDN in front. If a
custom domain (e.g. `pay.negocio.com`) or a proxy is later put in front of
**only** the customer-facing routes:

- `Runtime._payment_public_base_url` (`backend/src/payments/runtime.py`) is
  the one accessor that should start reading the new domain from config —
  either a new parameter/env var, or a second derivation path once the
  custom-domain mapping is known.
- `Runtime._webhook_notification_base_url` must **not** follow that change.
  Mercado Pago calls the webhook URL directly; it must keep resolving to the
  raw API Gateway origin, not a domain/proxy Mercado Pago never sees. This is
  exactly why the two accessors — and their local-dev env var fallbacks,
  `PAYMENT_PUBLIC_BASE_URL` and `PUBLIC_API_BASE_URL` — were kept distinct
  instead of merged into one, even though they resolve to the same value
  today.
- Custom domains per business are explicitly deferred scope (see
  `docs/ARCHITECTURE.md`); this section documents the seam, it does not
  implement the feature.

## Local development environment variables

`Runtime` accepts `public_api_base_url` as a constructor argument, always
preferred when present (set by the Lambda handlers from the live request).
The environment variables below are **fallbacks for local/test harnesses that
build a synthetic event with no `requestContext.domainName`** (see
`work/local_sandbox_server.py`), not something a real deploy needs to set —
`backend/template.yaml` does not define either for the deployed functions:

| Variable | Used by | Precedence |
| --- | --- | --- |
| `PAYMENT_PUBLIC_BASE_URL` | Staff-generated `paymentUrl`, the Mercado Pago `return_url` | Only read if `public_api_base_url` was not passed in. |
| `PUBLIC_API_BASE_URL` | The Mercado Pago `notification_url` (webhook target) | Only read if `public_api_base_url` was not passed in. |

## Sandbox blockers vs. real-money blockers (kept separate on purpose)

**Blocks the sandbox pilot today:**
- PR #2 is not merged, so nothing in this runbook can execute yet.
- The repository secrets/variables above do not exist yet.
- The AWS OIDC role and its trust policy do not exist yet.
- Mercado Pago sandbox refunds return HTTP 401 (`Unauthorized use of live
  credentials`) — tracked and left in the review queue, not simulated; see
  `docs/delivery/2026-09-11-canonical-payment-core-slice.md` and
  [the pilot readiness audit](2026-09-13-pilot-readiness-audit.md) (finding E1)
  for the retained evidence. Full pilot sign-off should not claim refunds
  work until this clears.
- Still-pending findings from the pilot readiness audit's second pass — see
  [that audit](2026-09-13-pilot-readiness-audit.md) section D in full before
  accepting sustained pilot traffic. P1–P4 and P6 (by-id lookups,
  work-queue pagination, concurrent-refund locking, the
  charge-cancellation-vs-payment race, and partial-refund Decimal handling)
  are now resolved (R21–R27); P5 (unbounded recursion under sustained
  DynamoDB throttling in three write paths) is not.

**Additionally blocks accepting real money (do not clear these for the
sandbox pilot):**
- No production Mercado Pago credentials exist or should be created yet.
- No production Secrets Manager values exist.
- No custom domain, production identity-provider tenant, or production
  frontend hosting exists — all explicitly deferred until after the sandbox
  pilot is qualified.
