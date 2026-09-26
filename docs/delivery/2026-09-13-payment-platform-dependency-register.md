# Payment platform dependency register

Status: pilot-readiness tracker, updated 2026-09-13. The current architecture
remains the product authority; this register distinguishes code that is already
implemented from qualification and account-level work that cannot be completed
inside the repository.

## Delivered in the repository

| Capability | Current state | Verification |
| --- | --- | --- |
| Public payment link | Opaque, revocable link; server-owned amount/currency; generic branded invalid-link page; idempotent Checkout Pro attempt and recovery. | Unit and Moto integration suites. |
| Tenant and staff access | JWT authorizer parameters, server-side membership/role checks, session bootstrap, customer/charge/summary/detail/work-queue/review/settings read models. | Authorization and transport tests; frontend typecheck/build. |
| Financial persistence | DynamoDB is the active runtime store. Payment identity, allocation, adjustment, cancellation and refund invariants use conditional or transactional writes. | Repository and end-to-end Moto tests, including deterministic conflict cases. |
| Provider processing | Durable webhook capture, leased provider-event/reconciliation/refund/outbox workers, bounded retries and explicit review states. | Worker tests and DynamoDB lifecycle integration test. |
| Staff UI | Customer and charge creation, shareable payment URL, charge evidence, cancellation, sandbox refund request, review resolution and tenant branding tokens. | Vitest, TypeScript and production build. |
| Honest list/summary behavior | Dashboard totals scan every tenant charge; capped customer and review responses expose `hasMore` and the UI displays the limit. | Repository/transport tests and frontend build. |
| Operational baseline | Structured redacted worker-failure logs, 30-day log retention, health alarms, scoped SES identity and per-function DynamoDB IAM actions. | Unit test for redaction; `sam validate --lint`. |
| Delivery pipeline | GitHub Free-compatible verification and manual pilot deployment workflows; OIDC credentials are requested only after validation/build. | Workflow YAML parse and local equivalent checks. |
| ETL retention, purge and apply (2026-09-19) | Row chunks expire by DynamoDB TTL (30-day pilot config); controlled purge; explicit, idempotent, resumable apply that creates only open Customers/Charges; bounded, cancellable frontend HTTP. See `2026-09-19-etl-retention-and-apply.md`. | Moto integration suites, Vitest, one manual fixture-mode browser walkthrough. **Not exercised against real AWS.** |
| Historical PostgreSQL reference | Retained under `backend/legacy/`; excluded from every Lambda `CodeUri`. | Historical suite collects and skips cleanly without `TEST_DATABASE_URL`; source-tree assertion. |

## Remaining repository qualification

| Priority | Item | Completion evidence |
| --- | --- | --- |
| P0 | Browser-level pilot acceptance for public link states, staff sign-in/session restoration, create/share, cancellation and review flows. | Deterministic browser run against the deployed sandbox with screenshots/logs and no fixture mode. |
| P0 | Real DynamoDB refund concurrency qualification. The double-opt-in test already exists and is never run accidentally. | Run `backend/tests/integration/test_real_dynamodb_refund_concurrency.py` against a disposable real table after deployment; record the result. |
| P1 | Re-run the complete regression battery in GitHub on the exact commit intended for deployment. | Required GitHub Actions checks green on the PR/`main`. |
| P0 | Real-DynamoDB qualification of ETL TTL expiry, apply and purge (transactions, leases, concurrency) and a browser run of the apply flow against the deployed sandbox. | Disposable real table + recorded result; screenshots/logs, no fixture mode. |
| P0 | Retention decision for imported rows and for the Customers created by apply (30 days is a sandbox value). | Written decision; parameter set accordingly. |
| P1 | Payment links for imported charges; guided conflict resolution. | Design + tests; not part of this branch. |
| P2 | Revisit the bounded `WorkIndex` claim scan only if a pilot can accumulate roughly 2,000 stale/index-visible items ahead of eligible work. | Load test demonstrates the cap is material, followed by cursor/cleanup design and regression coverage. |

P13 (local Python 3.14 versus Lambda Python 3.12) is an environment mismatch,
not missing product code. CI already uses Python 3.12. P14 (`sam build
--use-container`) becomes necessary only if a native dependency is introduced.

## External or account-level gates — do not fake these

| Gate | Required decision or access | Blocks |
| --- | --- | --- |
| GitHub publication | Push the reviewed commits, update/merge PR #2, and ensure the deployment workflow exists on the default branch. | GitHub verification and `workflow_dispatch`. |
| AWS deployment identity | OIDC IAM role restricted to this repository/branch, plus repository-level `AWS_REGION`, `AWS_DEPLOY_ROLE_ARN`, `APPLICATION_SECRET_ARN` and `PROVIDER_SECRETS_ARN_PATTERN`. | First stack deployment. |
| Staff identity | Real issuer, audience, pilot users and membership provisioning/revocation process. | Staff login and tenant authorization acceptance. |
| Operations | Verified SES sender and SNS alarm topic with a confirmed human subscriber. | Notification and incident-response acceptance. |
| Mercado Pago sandbox | Verified test seller and resolution of the provider's refund HTTP 401. | Full/partial refund success qualification; real-money use remains prohibited. |
| WhatsApp delivery | Provider, approved template, recipient opt-in and delivery/retry policy. | Automated WhatsApp sending only; copying/sharing a link remains available. |
| Production scope | Credentials, custom domain and explicit approval for live money; fiscal/CFDI and second providers remain deferred. | Any production launch. |

## Immediate sequence

1. Finish local regression and review the 26 unpushed commits.
2. Push the branch and let GitHub verification run; resolve findings before merge.
3. Create/configure the OIDC role, IdP, Secrets Manager references, SES identity
   and SNS topic using the deployment runbook.
4. Merge to `main`, deploy the sandbox manually, then execute browser acceptance
   and the opt-in real-DynamoDB concurrency check.
5. Invite a narrowly scoped pilot only after those results are recorded. Keep
   refunds in review until Mercado Pago's sandbox 401 is resolved and retested.
