# Platform ETL import jobs — API and persistence

> **Actualización 2026-09-19.** Este documento describe la rebanada de
> validación y su estado original. Desde entonces
> [`2026-09-19-etl-retention-and-apply.md`](2026-09-19-etl-retention-and-apply.md)
> añade TTL y purga de las filas temporales, el apply explícito y los estados
> `applying/applied/review/purging/purged`, y **reemplaza** lo que abajo dice
> sobre: los estados (§3), la retención indefinida de filas (§7) y que ningún
> código pueda crear Customers/Charges (§9), y la lista de pendientes (§11,
> puntos 3, 5, 6 y 7). Se conserva el texto original como historia de la
> rebanada de validación.

Status: delivered on `codex/super-admin-implementation`, on top of `92b3a45`
(charges-list fix) and the console skeleton in `4aeeb77`. This document
describes exactly one slice: **the import job now persists, server-side
validated, and auditable.** It does not create Customers or Charges.

**`validated` means the batch satisfies the input contract. It does not
mean any Customer or Charge was created.** Nothing in this slice writes to
the financial domain. Read this document alongside
`docs/delivery/2026-09-13-pilot-readiness-audit.md` (general pilot status)
and `docs/ARCHITECTURE.md` (product/architecture authority); neither of
those is amended by this slice, since it adds a new, self-contained
capability rather than changing existing payment behavior.

## 1. HTTP contract

Three routes, all behind the existing `StaffJwtAuthorizer` (proves identity)
plus a server-side platform-group check on top of that (proves platform
access — see §6):

```
POST /platform/businesses/{businessId}/imports/validate
GET  /platform/businesses/{businessId}/imports/{importId}
GET  /platform/businesses/{businessId}/imports?limit=50&cursor=<opaque>
```

`POST .../validate` request:

```json
{
  "source": {"fileName": "cartera-septiembre.csv", "format": "csv"},
  "profile": {"name": "Cobranza estándar MX", "delimiter": "comma", "dateFormat": "iso", "decimalSeparator": "dot", "currency": "MXN"},
  "records": [
    {
      "sourceRow": 2,
      "customer": {"externalId": "CLI-1001", "displayName": "Ferretería del Norte", "email": "cobros@example.com"},
      "charge": {"externalId": "FAC-2026-018", "amountMinor": 1250000, "currency": "MXN", "description": "Material de septiembre", "dueDate": "2026-09-30"}
    }
  ]
}
```

Headers: `Authorization` (required, JWT), `Idempotency-Key` (required, same
`SUBMISSION_KEY_RE` charset as every other staff/platform mutation in this
codebase — 8-128 chars of `[A-Za-z0-9._:-]`).

Response (`200`, both `validate` and the two `GET` routes return this same
shape — a list item is the same object, not a slimmed-down summary):

```json
{
  "importId": "...", "businessId": "...", "status": "validated",
  "source": {"fileName": "cartera-septiembre.csv", "format": "csv"},
  "profile": {"name": "Cobranza estándar MX", "delimiter": "comma", "dateFormat": "iso", "decimalSeparator": "dot", "currency": "MXN"},
  "summary": {"inputRows": 1, "validRows": 1, "errorRows": 0, "totalMinor": 1250000},
  "issues": [], "issuesTruncated": false,
  "createdAt": "...", "validatedAt": "...", "createdBySubject": "..."
}
```

`GET .../imports` response: `{"items": [...same shape...], "hasMore": bool,
"cursor": string | null}`. `cursor` is opaque (base64 of the DynamoDB
`LastEvaluatedKey`, see §2) — pass it back verbatim as `?cursor=` to get the
next page. `limit` is clamped to `[1, 100]`, default `50`.

Errors: `400 {"error":"invalid_request"}` for any structural/business-rule
violation (never a raw exception message or a DynamoDB error code — see
§7/§8); `403 {"error":"forbidden"}` when the caller is not in the exact
configured platform group, identical whether the businessId exists or not
(§6); `409 {"error":"operation_conflict"}` when the same Idempotency-Key was
already used with a different payload, or a conflict could not resolve
after bounded retries (§5); `404 {"error":"not_found"}` for an unknown
`importId`. `Cache-Control: no-store` on every response.

**409 is a real, typed exception path, not a side effect of exception-text
matching.** `DynamoRepository.create_import_job` raises
`payments.imports.ImportIdempotencyConflict` — a standalone exception type,
deliberately *not* a `ValueError` subclass — from both the places it can
detect a genuine key/payload conflict: the pre-read guard check, and the
re-read after a `TransactWriteItems` cancellation. `platform_transport.py`
catches that exact type, before its generic exception handler, and maps it
to `409`. Every other structurally-invalid request still raises a plain
`ValueError` and still maps to `400`. This was corrected during the
independent review that produced this document's 2026-09-14 revision: the
conflict was previously raised as a plain `ValueError`, indistinguishable
from a structural `400`, which contradicted this very document. See
`backend/tests/integration/test_import_jobs.py`'s "Hallazgo 4" section for
the real-HTTP-over-Moto tests covering this.

### Deviation from the routing sketch in the request

The request that specified this slice sketched `GET
/platform/businesses/{businessId}/imports/{importId}` and a separate `GET
.../imports?limit&cursor` as two routes. Both are implemented exactly as
sketched — API Gateway's HTTP API natively supports two GET routes on the
same resource differing only by a literal path segment vs. a `{param}`, so
no adaptation was needed here, unlike the "if the router can't do this,
adapt and document" contingency that sketch anticipated.

## 2. DynamoDB model

All four entities for one import live under the **same business
partition** (`PK = BUSINESS#{businessId}`), unlike the request's own
reference sketch (which put rows under a separate `PK = IMPORT#{importId}`
partition). That sketch was deliberately not followed: keeping everything
under the business partition makes "no import job is ever reachable from
another business's queries" a property of the key structure itself
(`PK` equality), not something every future read path has to remember to
check. No new GSI was added — `LookupIndex`, `SubjectIndex`, `WorkIndex` are
untouched.

| Entity | PK | SK | Notes |
| --- | --- | --- | --- |
| Metadata | `BUSINESS#{businessId}` | `IMPORT#{createdAtIso}#{importId}` | One item; everything the HTTP response needs. `createdAt` first in the SK makes `Query` + `ScanIndexForward=False` return newest-first for free. |
| Idempotency guard | `BUSINESS#{businessId}` | `IMPORT_KEY#{sha256(subjectId + "\n" + idempotencyKey)}` | `{importId, payloadDigest, metadataSk}`. No secondary "lock" item (unlike refunds) — two *different* idempotency keys creating two jobs concurrently is not a conflict to prevent. |
| By-id lookup | `BUSINESS#{businessId}` | `IMPORT_LOOKUP#{importId}` | `{metadataSk}`. See below for why this exists instead of reusing `LookupIndex`. |
| Row chunks | `BUSINESS#{businessId}` | `IMPORT_ROWS#{importId}#{chunkIndex:04d}` | Up to 25 rows per chunk (`imports.ROWS_PER_CHUNK`); never read by any endpoint in this slice — pure storage for a future apply step. |
| Audit | `BUSINESS#{businessId}` | `AUDIT#import.{status}#{guardKey}` | `action` is `import.validated` or `import.invalid` — it reflects the job's real status, never hardcoded to "validated". `guardKey` is `sha256(subjectId + "\n" + idempotencyKey)`, the same value the idempotency guard item's own SK uses — **not** the raw `idempotencyKey`, because two different superadmins can legitimately reuse the same key value in the same business partition, and a raw-key SK would collide between them (see §5's "independent review correction" note). |

**Why a fourth item (`IMPORT_LOOKUP`) instead of reusing `LookupIndex` (the
GSI every other `_by_id`-style lookup in this codebase goes through):** a
GSI is never eligible for `ConsistentRead`. A staff member who just
validated a batch and immediately reopens it (or the Auditoría tab
re-fetching moments after a submit) must never see a transient miss. This
mirrors R30/R34 from the pilot readiness audit — the exact same lesson,
applied here from the start instead of needing a second pass to fix it.
`IMPORT_LOOKUP` is one extra ~100-byte item, resolved with the same
`ConsistentRead=True` `GetItem` (`DynamoRepository._get`) every other
strongly-consistent read in this file already uses.

**Listing** is a `Query` on `PK` + `begins_with(SK, "IMPORT#")`, never a
`Scan`, with real pagination: DynamoDB's own `Limit` + `LastEvaluatedKey`,
not an application-level slice of an already-fetched list. The cursor
returned to the client is `base64(json({"PK":..., "SK":...}))`; on the way
back in, the server checks the decoded `PK` matches the business in the URL
before ever passing it to DynamoDB as `ExclusiveStartKey` — a forged or
stale cursor for a different partition is rejected (`400`), not silently
followed.

**Size limits** (see §4 for the exact numbers and why): the creation
transaction is guard + metadata + lookup + audit + up to 20 row chunks (500
rows ÷ 25/chunk) = at most 24 items, ~700KB total in the worst case —
comfortably under DynamoDB's `TransactWriteItems` ceilings (100 items,
4MB). No single item ever holds the whole batch.

## 3. ImportJob states

`payments/imports.IMPORT_STATES` was `("validated", "invalid", "committing",
"completed", "failed", "cancelled")` in this slice and is now `("validated",
"invalid", "applying", "applied", "review", "purging")` (see the 2026-09-19
document). **This slice only ever produced `validated` or `invalid`.** The other four exist so a future "apply" slice
does not need to change this contract's shape — only add the transitions
into them. No code path in this slice can reach `completed`; there is no
commit/apply endpoint.

## 4. Pilot limits (`payments/imports.py`)

| Limit | Value | Why |
| --- | --- | --- |
| Rows per import | 500 | Keeps the creation transaction's item/size math (§2) comfortably bounded without needing per-request math to prove it every time. |
| Rows per stored chunk | 25 | 500 rows ÷ 25 = 20 chunk items max; each chunk (25 rows × worst-case field lengths) stays well under the 400KB item ceiling. |
| Issues sample (response + stored) | 50 | `summary.errorRows` is always the *exact* full count regardless of truncation; `issues` is capped and `issuesTruncated` is `true` when the batch has more than 50 total field-level problems. Per-row stored detail is separately bounded by the fixed, finite set of validation rules (~9 possible issues per row) — not by this constant — so storage size never grows unboundedly with row count either way. |
| `customer.externalId` / `charge.externalId` | 120 chars | |
| `customer.displayName` | 160 chars | Matches `StaffService.create_customer`'s existing limit for the same field. |
| `customer.email` | 254 chars | RFC 5321 practical ceiling, matches `StaffService`'s existing check. |
| `charge.description` | 240 chars | Matches `StaffService.create_charge`'s existing limit. |
| `amountMinor` | 1 – 500,000,000 (minor units; $5,000,000.00 MXN) | A generous but explicit pilot ceiling. Always a real `int`; `bool` is explicitly rejected (Python's `bool` is an `int` subclass) and `float` is never accepted — money is never represented as a float anywhere in this path. |
| `currency` | Exactly `"MXN"` | Matches the rest of the pilot. |
| `dueDate` | ISO `YYYY-MM-DD`, a real calendar date | `date.fromisoformat` rejects e.g. `2026-02-31`. |
| `sourceRow` | Positive `int`, ≤ `imports.MAX_SOURCE_ROW` (100,000) | **Independent review correction**, 2026-09-14: a malformed `sourceRow` (a float, `bool`, string, list, object, negative, zero, or one above the ceiling) makes that row `invalid`, exactly like any other field-level problem — it never rejects the whole request with a structural `400`, and it never reaches storage as the raw unvalidated value. `rowResults[i].sourceRow` is always either the validated positive `int` (valid row) or `0` (invalid row, no reliable row number) — see §7. This matters specifically because `boto3`/DynamoDB rejects a raw Python `float` outright (`TypeError: Float types are not supported`), which previously meant a single adversarial or malformed `sourceRow` (e.g. `2.5`) could make `create_import_job`'s entire transaction fail before anything was stored, silently dropping the whole batch instead of recording it as `invalid`. |

Oversized or malformed **requests** (not `records` array too long, `source`/
`profile` failing their own structural checks, or an idempotency key that
does not match the existing format) are rejected with `400` **before** any
DynamoDB transaction is attempted — never a partially-started expensive
write.

## 5. Idempotency semantics

Scope: `(businessId, subjectId, idempotencyKey)` → at most one ImportJob,
ever. The guard item's own `attribute_not_exists(PK)` condition inside the
same `TransactWriteItems` call that creates everything else is what makes
"exactly one" a guarantee under real concurrency, not just a pre-check race
(the same pattern `create_refund_operation` already uses, and the same
recovery path it uses after a conflict: re-read the guard with
`ConsistentRead`, and if the payload digest matches, return the winner
exactly — never a second job, never an error, for the same logical retry).

- **Same key + same business + same subject + same payload** → returns the
  existing job (its `importId`, unchanged), whether this is a clean retry
  or two requests that raced each other.
- **Same key reused with a different payload** → `409` via
  `ImportIdempotencyConflict` (see §1), explicit conflict, never silently
  overwrites or creates a second job under the same key.
- **Two different keys, same/overlapping content** → two different jobs,
  by design. That is not a conflict to prevent — different keys are
  different logical submissions, even with identical content (e.g. an
  intentional re-check).
- **Two different subjects reusing the same key value, same business**
  (**independent review correction**, 2026-09-14) → two different jobs,
  each keyed by `(businessId, subjectId, idempotencyKey)` as this section
  always documented. This was previously broken in practice: the *audit*
  item's SK (§2) was keyed by the raw `idempotencyKey` alone, so the
  second subject's transaction failed with an unhandled `RuntimeError`
  even though the guard/metadata items were correctly subject-scoped. Fixed
  by keying the audit SK off the same subject-scoped `guardKey` the guard
  item uses. Covered by
  `test_two_subjects_sharing_the_same_business_and_idempotency_key_create_two_distinct_jobs`
  and `test_retrying_the_same_subject_and_key_returns_the_same_job_and_does_not_add_a_second_audit`.
- **The payload digest** (`payments/imports.canonical_digest`) is computed
  *after* the same deterministic normalization every record goes through
  during validation (whitespace collapse, email lowercasing/trim) — so two
  requests that differ only in incidental formatting hash identically,
  while anything that would change validation output also changes the
  digest. Unrecognized extra keys anywhere in the request never reach the
  digest (see §7/§8's "unexpected properties" policy) and so can never be
  used to force a digest collision or divergence.
- **No secondary lock item.** Unlike `create_refund_operation` (which needs
  a `REFUND_LOCK#{paymentId}` because *different* idempotency keys racing
  for the same payment genuinely must be prevented), there is no such
  cross-key resource to protect here — see the third bullet above.

## 6. Superadmin authorization

Every `/platform` route:

1. `StaffJwtAuthorizer` (existing HTTP API JWT authorizer) validates the
   token's signature/issuer/audience before the Lambda ever runs.
2. `payments/platform_auth.platform_role(event)` — shared with
   `staff_transport.py`'s `/session` response, extracted into its own
   module this pass specifically so this check exists in exactly one place
   — reads `requestContext.authorizer.jwt.claims["cognito:groups"]` (a
   claim only API Gateway's authorizer can set) and compares it to
   `PLATFORM_ADMIN_GROUP` (env, default `cauce-super-admin`) by **exact set
   membership**, never substring/prefix matching. `"cauce-super-admin-x"`
   or `"cauce-super-adminx"` do not match `"cauce-super-admin"`.
3. `PlatformService.authorize(subjectId, platformRole)` re-checks that
   value defensively before any business-scoped work — including before
   the business-existence check itself, so an unauthorized caller gets a
   generic `403` and learns nothing about whether the target `businessId`
   is even real.

`platformRole` is **never** read from the request body, query string, or
any header — only from the verified JWT claim. A client that sends
`{"platformRole": "super_admin"}` in the body, or an
`X-Platform-Role` header, has zero effect (covered by
`test_platform_role_cannot_be_supplied_by_the_client` and
`test_a_role_header_can_never_substitute_for_a_verified_group_claim`).
Hiding the "Implementación" nav entry and gating `/platform/*` client-side
(`PlatformAdminShell`, already in `4aeeb77`) is UX only — it grants nothing
by itself, and every route re-derives authorization server-side regardless
of what the browser shows.

**Platform authorization does not imply tenant membership, and the console
now reflects that** (independent review correction, 2026-09-14). A
superadmin can legitimately have `memberships=[]` — platform access and
business membership are independent by design (this section) — but the
console previously only ever read `session.memberships[0]` to pick a
destination `businessId`, so such a superadmin could never select a
business to validate against, and the Auditoría tab could sit in a
misleading indefinite loading state (a TanStack Query `useQuery` left
permanently `enabled: false` never resolves `isPending` to `false`). The
console (`ImplementationPage`'s `BusinessSelector`) now always exposes a
manual `businessId` field, in addition to any memberships as convenience
options; nothing about this adds a business-listing endpoint, a `Scan`, or
a new GSI — the server still independently verifies the `businessId`
exists via the normal `404`/business-not-found path, exactly as before.
Switching the selected business clears any in-progress job/result/error
from the previous one, and derives a fresh idempotency signature (§5, and
the frontend note below) — never a mix of two businesses' state.

## 7. What is stored

- Metadata: `businessId`, `status`, `source` (fileName, format),
  `profile` (name, delimiter, dateFormat, decimalSeparator, currency),
  `summary` (row counts, total minor amount), an **issues sample** (≤ 50
  entries, each `{row, field, message}` — field names and short guidance
  text, e.g. `"El correo no tiene un formato válido."`), `issuesTruncated`,
  timestamps, and `createdBySubject` (the JWT `sub` — an opaque subject id,
  not a name or email).
- Row chunks: the same normalized `{sourceRow, customer, charge}` shape the
  HTTP request sent, chunked (§2/§4) — including customer external
  id/display name/email and charge amount/description/due date, since a
  future apply step needs this to actually create records. This is
  financial/contact working data for an *internal staff tool gated to
  platform admins*, not public or tenant-staff-visible data. **As of 2026-09-19 these
  items carry a DynamoDB `ttl` (30 days, pilot config) and can be purged;
  when this slice was written they were retained indefinitely.** That is tracked as explicit debt in §11, not resolved
  by this pass; do not read anything in this document as implying PII
  retention is handled.
- **A row is never stored with the raw, unvalidated value the client
  sent** (independent review correction, 2026-09-14). `rowResults[i]` is
  built from the *validated* record, never from the raw request: an
  invalid row's `sourceRow` is always `0`, and an invalid row's `customer`/
  `charge` fields are never copied into storage at all (`normalized` is
  `null` for an invalid row). A `float`, `bool`, string, list, or object
  submitted for `sourceRow` — or a `float` submitted for `amountMinor` —
  never reaches DynamoDB in any form; it only ever produces a validation
  issue and an `invalid`-status job (§4).
- **`customer.externalId` reuse within one batch** (independent review
  correction, 2026-09-14): the same `externalId` may repeat across rows —
  that is the normal multiple-charges-per-customer case — but only if every
  occurrence's `displayName` matches exactly (post-normalization) and every
  occurrence's non-null `email` matches (a missing email never conflicts
  with an earlier provided one). If the same `externalId` appears with an
  incompatible name or email, the batch is `invalid` and the issue
  identifies both the original row and the conflicting row. This check is
  **within the submitted batch only** — it does not compare against
  Customers already stored for the business. Comparing against existing
  Customers is explicit debt for the apply step (§11), not implemented
  here.
- An audit entry per successful creation: action (`import.validated` or
  `import.invalid` — see §2; it always reflects the job's real status, it
  is never hardcoded to "validated" even when the batch failed validation),
  aggregate id (the `importId`), the idempotency key as `operation_key`,
  the actor's subject id, a timestamp, and the same summary counts — no
  row-level detail.
- **Unrecognized/extra properties** anywhere in the request (unexpected
  top-level keys, or extra keys inside `source`/`profile`/`customer`/
  `charge`) are **silently ignored**, not rejected and not stored — a
  deliberate, documented policy (`payments/imports.py`'s module docstring
  and inline comments), not an oversight: this receives the frontend's own
  canonical payload, and failing hard on a harmless additive field from a
  future frontend version would make every import fail for no real reason.
  Ignoring them also means they can never influence the idempotency digest.

## 8. What is never stored (or logged)

- No raw uploaded file content, no full request body dump — only the
  normalized, field-by-field data described in §7.
- No customer email/name/external id, and no arbitrary exception text, in
  application logs — the existing structured-worker-log discipline
  (`payments/workers.py`, from the prior pilot-readiness pass) extends to
  this slice by construction: nothing in `platform.py`/
  `platform_transport.py` logs anything at all; errors become one of the
  four generic HTTP error bodies in §1, never a raw exception message,
  stack trace, or DynamoDB error code.
- No `platformRole` (or anything else auth-related) is ever accepted from
  client input (§6) — so there is nothing client-supplied to (accidentally)
  persist there either.
- No S3 object, no presigned URL, no `.xlsx` binary — none of that exists
  in this slice (§9/§11).

## 9. Why this still does not create Customers or Charges

Explicitly out of scope for this pass, by instruction:

- No `Customer` or `Charge` row is ever written by any code path reachable
  from `/platform/*`. `payments/platform.py` and `payments/imports.py`
  import nothing from `payments/staff.py`, and `DynamoRepository`'s new
  methods (`create_import_job`, `import_job_detail`, `list_import_jobs`)
  never call `create_customer`/`create_charge`.
  `test_a_batch_with_errors_creates_an_invalid_job_and_writes_no_customer_or_charge`
  (Moto integration) asserts this directly, not just by code inspection.
- No historical payments or refunds are inferred or imported.
- No rollback exists because there is nothing transactional-and-financial
  yet to roll back — an ImportJob is not a financial record.
- **`validated` is a statement about the *input*, not about the domain
  state.** A `validated` job means: every row satisfied the contract in
  §4, server-side, independently of whatever the browser's own dry run
  said. It does not mean any customer or charge exists because of it.

## 10. Frontend console notes (`staff-workspace`)

Added or corrected during the same 2026-09-14 independent review as the
backend corrections above; each is covered by a dedicated unit test rather
than only by inline reasoning.

- **Idempotency signature** (`implementation-utils.batchSignature`) is a
  pure, exported, `async` function computing SHA-256 (via Web Crypto's
  `crypto.subtle.digest`, not a rolling hash) over the *complete* payload
  the request will actually send: `businessId`, `source.fileName`,
  `source.format`, the full `profile` (including `currency`), the mapping
  (serialized in a canonically sorted key order, so JSON property
  insertion order can never change the signature), and the source text.
  Previously this omitted `fileName`/`source.format` while the backend's
  own digest (§5) included them, so two sources with identical content but
  a different name or format reused the same `Idempotency-Key` and hit a
  spurious `409` on the very first submission of the second one.
- **Auditoría cache invalidation**
  (`implementation-utils.platformImportsQueryKey`): the submit path and
  `ServerImportsPanel`'s read both derive the TanStack Query key from this
  one shared function, and `runDryRun` calls
  `queryClient.invalidateQueries({ queryKey: platformImportsQueryKey(...) })`
  — targeting exactly that key, never the whole cache — after a
  successful, persisted `validateImport` response only (never on a failed
  or unsent submission). Previously nothing invalidated this query at all,
  so a just-submitted job could be invisible in Auditoría until
  `staleTime` elapsed.
- **`fixturePlatformApi`** (`fixtures/platform-fixtures.ts`, used only in
  fixture/dev mode, never against the real server) now scopes every stored
  job by `(businessId, subject, idempotencyKey)` and compares a canonical
  digest of the payload, raising the same `ApiError(409,
  "operation_conflict")` shape the real server returns for a reused key
  with a different payload; `getImport`/`listImports` genuinely filter by
  `businessId`. Previously the fixture indexed only by `idempotencyKey`,
  never compared the payload, and both read methods ignored `businessId`
  entirely — so it could not reproduce the real contract's isolation or
  conflict behavior, undermining any dev-mode testing against it.

## 11. Next slice (explicitly not this one)

In roughly the order that unblocks the next:

1. **Secure temporary upload** — a real object store path for files larger
   than fits comfortably in a synchronous Lambda request body (S3 +
   presigned URLs, or equivalent), with a lifecycle policy so nothing
   uploaded-but-abandoned lingers indefinitely.
2. **Server-side `.xlsx` parsing** — today the browser only ever sends
   CSV/TSV text or pasted cells; binary `.xlsx` parsing belongs in the
   backend (the frontend's own copy already says so), reusing this same
   validate contract once a file lands via (1).
3. **Confirmation/apply through the existing domain services** — a new,
   separate endpoint (deliberately *not* added this pass, and not added by
   the independent-review pass either) that takes an already-`validated`
   `importId` and, through `StaffService`'s existing
   `create_customer`/`create_charge` (idempotent, already durable), turns
   each stored row into a real domain write — with its own idempotency,
   its own partial-failure semantics (what happens when row 340 of 500
   fails?), and its own audit trail distinct from `import.validated`.
4. **Durable, queryable row-level audit** — right now, an import's own row
   chunks are pure storage with no read endpoint; once apply exists, staff
   need to see exactly which rows became which Customer/Charge, which
   failed, and why.
5. **Retention/TTL policy for stored row chunks** — §7 already flags this:
   row chunks hold customer contact data and charge amounts (working data
   for an internal staff tool, but still PII) and are currently retained
   indefinitely, with no DynamoDB TTL attribute and no scheduled or
   on-demand deletion path. **This is not resolved by the independent
   review pass** — that pass fixed correctness bugs in the existing
   persistence, it did not add retention controls. Needed before this
   slice could be considered pilot-ready on its own.
6. **Controlled deletion of temporary import data** — related to (5) but
   distinct: even with a TTL, there is currently no staff-facing or
   operator-facing way to delete a specific import's data on demand (e.g.
   a staff member pastes the wrong file, or a business offboards).
7. **Comparison against existing Customers for `customer.externalId`** —
   §8 documents an *intra-batch* identity-conflict check (the same
   `externalId` must carry a consistent name/email within one submitted
   batch). It does **not** compare against Customers already stored for
   the business. That comparison — deciding whether a batch row matching
   an existing Customer is an update, a legitimate new charge, or a real
   conflict — belongs to the apply step (3), once real domain writes are
   in play, not to validation of an as-yet-unapplied batch.
