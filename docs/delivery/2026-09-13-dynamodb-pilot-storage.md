# DynamoDB pilot storage decision

Status: active implementation decision, 2026-09-13.

The pilot replaces the proposed managed PostgreSQL instance with one on-demand DynamoDB table. No data has been deployed, so this is a clean cutover rather than a data migration.

## Table contract

`PaymentsTable` has a composite primary key (`PK`, `SK`) plus three access indexes:

| Access pattern | Key / index | Safety property |
| --- | --- | --- |
| Tenant records — business, customers, charges, balances, attempts, payment history | `PK = BUSINESS#{businessId}` | All staff reads carry the server-authorized business ID. |
| Public link, connection, attempt and provider-payment identity | `GSI1` lookup identity | Opaque lookup keys are hashed or server-generated; no tenant enumeration. |
| Staff session memberships | `GSI2PK = SUBJECT#{subjectId}` | JWT subject is resolved server-side, then membership is re-read in the tenant partition. |
| Pilot worker backlog | `GSI3` work state/time | A conditional lease is acquired before work; scans are not used for financial writes. |

Financial operations use `TransactWriteItems` with condition expressions. The transaction includes the immutable financial fact, the idempotency/unique identity item, the balance mutation or adjustment, audit evidence, and any outbox item. The implementation must fail closed on a conditional check failure and then re-read the immutable record; it must never retry the write with a new financial identity.

## Cost posture

The table uses on-demand capacity, server-side encryption, point-in-time recovery, and deletion protection. It has no always-on database instance or NAT Gateway. The meaningful pilot cost becomes actual request/storage volume rather than a monthly database floor.

## Delivery gates and current status

1. **Complete** — the runtime, unit suite, and DynamoDB integration acceptance suite use `DynamoRepository`.
2. **Complete** — tenant onboarding and explicit merchant verification use the table.
3. **Complete in source control** — CI has no PostgreSQL service and the guarded GitHub deployment workflow creates the table and grants scoped Lambda access. No AWS resources have been created from this change.
4. **Pending deployment configuration** — choose and provision staff identity, HTTPS frontend origins, sandbox merchant secrets, verified SES sender, and an alarm subscriber; then execute the sandbox charge-to-payment pilot.

PostgreSQL remains in the worktree only as a historical reference implementation and skipped legacy integration suite. It is not a runtime, CI, or deployment dependency after the cutover.
