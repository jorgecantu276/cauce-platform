# Payment platform — current architecture

Status: active design baseline, 2026-09-11. This describes the intended product; it does not claim the system is implemented or production-ready.

## Product and repository ownership

Build a collections workspace for local businesses in northern Mexico that repeatedly collect payments from known customers. A business creates a charge, shares its payment link, and tracks the outstanding balance and payment history. Recurring collections initially means staff-created repeat charges; it does not mean automatic card debits.

- **cauce-platform** owns the current product, deployable code, and this architecture.
- Other repositories and recovered applications are not dependencies of Cauce Platform.
- [soluciones-escolares](https://github.com/jorgecantu276/soluciones-escolares) owns the independent school application. Its charge/payment/allocation model is prior art, not a shared dependency or an implementation specification for this product.

This document supersedes the September 9 design specification and implementation plan. The [September 11 architecture review](superpowers/plans/2026-09-11-payment-platform-architecture-review.md) remains historical risk evidence. It reviews the old plan; it is not a second active specification. Existing code and infrastructure are not changed by this decision.

## Smallest useful product

A manually onboarded business can manage customers, create MXN charges with descriptions and due dates, share a secure link, receive a Mercado Pago payment, and inspect balances and history. The customer sees the specific obligation, pays through hosted checkout, and returns to a status page. Provider confirmation determines payment status; the browser redirect never does.

The first slice supports full payment of one charge. The data model preserves separate payments and allocations so retries, extra payments, and future partial payments can be accounted for without overwriting history. Extra money is recorded and flagged for resolution; it is never silently discarded.

## Domain and invariants

| Record | Responsibility |
| --- | --- |
| Business and membership | Merchant identity and server-controlled user access |
| Customer / payer | Who owes the charge; payer details may identify someone else |
| Charge | The obligation: business, customer, immutable issued amount, MXN currency, description and due date |
| Payment attempt | One checkout attempt, with stable operation identity and provider checkout reference |
| Payment | A distinct provider-confirmed money movement, scoped to merchant account and test/live environment |
| Allocation | How much of a payment satisfies a charge |
| Provider event | Append-only evidence of a provider notification or reconciliation observation and its processing outcome |
| Financial adjustment | An auditable correction or reversal linked to its original records |

Amounts use integer minor units. Balances are derived from charges, allocations and effective adjustments. Payment reversals also reverse the associated allocation effect, without deleting the original records. Charge cancellation and payment refund are distinct operations. A folio is a display reference, not the financial model. Provider events are evidence, not a financial ledger by themselves.

## Deployment and access

Use one shared application/backend and Amazon DynamoDB for the pilot. The table uses `PK`/`SK` entity records, not one mutable document per tenant: business-owned records are partitioned by business, opaque links and provider identities have dedicated lookup keys, and subject memberships have a dedicated access index. Every tenant-owned record carries business_id; relationships must prevent references across businesses. Authenticated access resolves membership server-side. Client-supplied tenant IDs, hostnames and browser origins never confer authorization. Provider accounts and credentials are scoped by business and environment; secrets stay outside source control.

Financial writes use DynamoDB `TransactWriteItems`, conditional expressions, immutable audit/entity records, and deterministic idempotency keys. A charge balance is updated only in the same transaction that writes its allocation or compensating adjustment. A conditional folio counter creates the tenant-local display number. Provider payment ownership is represented by a unique identity record so the same provider payment cannot be accepted by two businesses. Worker leases are conditional and time-bounded. This is the DynamoDB equivalent of the former database-transaction and uniqueness-constraint guarantees; a scan-based worker claim is acceptable only for the small pilot workload and is explicitly not the long-term queue design.

Start with one platform domain and opaque, revocable charge links. Public links expose only the minimum checkout information and no customer account history. Retain AWS as the deployment target. The pilot backend has no VPC dependency: Lambda reaches DynamoDB and Secrets Manager through AWS-managed service endpoints, avoiding the fixed NAT Gateway cost. Cognito, public web hosting, production merchant credentials and a durable high-volume queue remain deployment decisions before a live pilot.

## Payment lifecycle and reliability

1. Staff creates a charge with a server-validated amount and business/customer relationship.
2. Opening its link reads the charge. Creating checkout persists an attempt before calling Mercado Pago; browser retries reuse that operation identity. An uncertain provider result is recorded for recovery.
3. Hosted checkout collects payment using the business's connected merchant account. The first provider is Mercado Pago; merchant connection and credential lifecycle must be verified before a live pilot.
4. Validate webhook authenticity according to the provider contract, durably capture evidence, and retrieve authoritative payment data. Check seller/account, environment, amount, currency and local reference before allocating funds. Acknowledge only after durable acceptance; failed durable acceptance must remain retryable.
5. In one DynamoDB transaction, conditionally claim the distinct payment identity, apply any valid allocation, update the charge balance, and persist outbox work for notifications. Conditional writes suppress repeated processing without hiding a second real payment.
6. Workers retry outbox tasks independently. Reconciliation recovers pending or uncertain operations and checks subsequent refunds/reversals. Out-of-order events must not regress state based only on arrival order. Unmatched or inconsistent payments enter an operator review queue.

The provider boundary covers checkout creation, authoritative payment retrieval and normalized events. Provider-specific references and statuses remain available. Do not build speculative adapters for Openpay/Banorte or Clip until their exact product and integration contracts have been selected.

## Deferred scope and implementation gate

Defer school modules, subscriptions/automatic debits, customer self-service accounts, custom merchant domains, automated CFDI, platform commissions/split settlements, a second payment provider, and sophisticated accounting reports. Deferred invoicing automation is a product scope decision, not a conclusion about merchant fiscal obligations.

**Refund initiation, clarified.** "Self-service" above means a *customer-facing* refund request; that stays deferred with everything else in customer self-service accounts. It does not describe the owner-only staff action. The staff workspace's authenticated, owner-only "Solicitar reembolso" control **is in scope for the sandbox pilot**: it creates a durable, idempotent refund operation (unique per payment while unresolved), processed by a leased worker against the connected sandbox Mercado Pago account, with its outcome visible in the charge's evidence timeline and, if the provider result is inconclusive, routed to the owner review queue rather than silently assumed. The button never implies a completed refund by itself — only a provider-confirmed adjustment does that. Required acceptance cases for this control before it is considered pilot-ready: a full refund; a partial refund; two refund requests for the same payment rejected while one is still unresolved (at most one active refund per payment); a refund request while a new provider snapshot for the same payment is also arriving (no double compensation); a completed refund correctly reopening the charge's checkout so a customer can pay again (see the DynamoDB payment lifecycle section above); and a provider rejection landing in the review queue instead of being reported as success. **This control is blocked for real money**: Mercado Pago sandbox has returned HTTP 401 on the refund endpoint during qualification (see [the canonical payment core slice delivery note](delivery/2026-09-11-canonical-payment-core-slice.md) and the pilot readiness audit); until that clears with the provider, no full end-to-end refund evidence exists, and production/live credentials must not be connected regardless.

The delivery slice is implemented against DynamoDB and is awaiting its deployment configuration and sandbox qualification. Required acceptance cases before accepting pilot payments remain: duplicate submission, lost checkout response, duplicate/out-of-order notifications, second actual payment, wrong account/amount/currency/environment, cross-business access, worker crash after payment commit, missed webhook recovery, and refund/reversal reconciliation. Do not execute the superseded plan's task list.

Current storage and delivery status: [DynamoDB pilot storage decision](delivery/2026-09-13-dynamodb-pilot-storage.md).

Pilot deployment configuration: [GitHub Actions and environment contract](delivery/2026-09-13-pilot-deploy-configuration.md).
