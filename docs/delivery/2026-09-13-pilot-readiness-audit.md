# Pilot readiness audit — 2026-09-13

Status: five remediation passes against `codex/canonical-payment-slice`,
starting at commit `1bdf6be`. As of the end of the fifth pass this branch is
**26 commits ahead of `origin/codex/canonical-payment-slice`**, none pushed,
no merges, no AWS/GitHub changes. This is evidence-based, not a completeness
claim: "resuelto" means
implemented and covered by a test that fails without the fix; "pendiente"
means identified with a concrete recommended fix but not implemented;
"dependencia externa" means no amount of local code change resolves it. **Do
not read "resuelto" as "no other defect exists in that area"** — this
document exists in its fifth revision specifically because THREE
consecutive rounds of independent review/supervision each found a real
defect the previous pass missed (R2's incomplete fix in the second pass; the
adjustment-status-transition bug in the third pass; and, in the fourth pass,
that the third pass's own fix for that bug was itself incomplete — `status`
alone was still being trusted as a proxy for whether money had moved, plus a
second, independent defect in the refund-idempotency race). The fifth pass
(see "C.1 Resuelto — quinta pasada de remediación local" below) closed P8,
P9, P11, P12 and P15 without an external review prompting it, and an
independent preflight afterward found no further defect in it — see the
preflight note at the end of this section. Treat it as a living record, not
a certificate — and see the explicit "still open" note at the end of this
section before treating refund correctness as closed.

A second Claude session was working in this same worktree concurrently
during the first pass. One partial, unfinished edit from that session (or a
prior attempt) was found in `backend/src/payments/dynamodb.py`
(`capture_provider_event`'s `GSI1PK`) and reverted rather than completed, per
explicit instruction not to ship a partial GSI-key migration — the full,
completed migration is now R23 below.

## Second pass (independent review correction)

An independent review of the first pass found that **R2 was not actually
fixed** — only half of it was. The first pass made `_claim_work` return the
item's pre-claim status to the *caller*, but it still persisted
`status="processing"` to DynamoDB during the lease. That breaks the state
machine the moment the lease is released: `mark_attempt_ready` re-reads
status fresh and rejects anything it doesn't recognize, and nothing ever
restored the original value, so an item could become permanently invisible
to future claims after being claimed exactly once. Reproduced with Moto
during the second pass: an `unknown` attempt's recovery failed outright, and
a `ready` attempt that reconciliation merely checked (nothing new from the
provider) was claimed successfully once and then never seen again by a
second `run_reconciliation_worker` pass. R21 below is the actual fix — status
is now never touched by a claim; `lease_token`/`lease_expires_at` alone are
the exclusion mechanism, matching the pattern every `finish_*` method in this
file already assumed.

The second pass also completed P1, P2, P3, P4, and P6 from the first pass's
pending list (R23–R27 below), and found and fixed one additional defect that
was not on any prior list: a single provider snapshot carrying two new
effective adjustments at once could make `_apply_existing_payment_snapshot`
build an invalid `TransactWriteItems` call (two operations on the same
Charge item — DynamoDB rejects this outright) and, independently of that,
could have restored more balance than was ever allocated if it hadn't been
rejected (R27).

## Third pass (another independent-review defect, plus completing P5/P7 and documenting P2's residual limit)

A second independent review — of the *second* pass's own work, specifically
`_apply_existing_payment_snapshot` (the same method R27 had just touched) —
found a real, reproducible defect neither pass had caught: `if
self._get(business_id, sk): continue` treated a `provider_adjustment_id`,
and its `status`, as immutable once first seen. Reproduced with Moto before
any fix: an adjustment observed first as `"pending"` (correctly not
effective, no restore) never restored balance even after a *later* snapshot
reported the same `provider_adjustment_id` as `"approved"` — the stored
adjustment stayed `"pending"` forever and the charge stayed fully allocated,
with no error and no evidence anything was wrong. `postgres.py`'s reference
(`ON CONFLICT (business_id, payment_id, provider_adjustment_id) DO UPDATE
SET status=EXCLUDED.status, ...`) already treats this as a mutable upsert;
the DynamoDB path never did. R28 below is the fix: 7 new tests, all
confirmed failing against the pre-fix code first, cover pending→approved,
rejected→approved, duplicate-approved idempotency, a stale/older snapshot
being unable to rewrite a newer one, a deterministic (not real-thread)
simulation of two racing observations, a second adjustment's restore being
capped by the Charge's already-reduced `allocated_minor` (not the original
Allocation amount), and a single transaction touching the Charge/Attempt at
most once each. One case has **no safe automatic rule and is a documented,
deliberate policy choice, not an inferred one**: an effective adjustment
later reported as non-effective (`"approved"`→`"rejected"`) has no safe
automatic reversal rule available from `MercadoPagoSandbox`, `postgres.py`,
or the existing architecture, so the already-confirmed financial effect is
retained rather than reversed, and the contradiction is surfaced as
`review_reason="adjustment_effective_reversed"` on the payment for a human
to resolve — see R28's own row above for exactly what it does and does not
decide.

This pass also: completed P5 (bounded, backoff-and-jittered retries
replacing unbounded recursion in the three write paths that had it — R29)
and P7 (a short, bounded retry for `mark_attempt_ready`/`mark_attempt_unknown`
against LookupIndex's eventual consistency — R30); and replaced the one
test flagged as an intermittent, undocumented-isn't-good-enough source of
CI non-determinism (R24's `ThreadPoolExecutor`-based refund-lock race) with
a deterministic equivalent that tests the same transactional condition
without relying on thread timing, keeping the original real-thread version
separately, skipped by default, for manual verification against real
DynamoDB after a deploy (R29 also documents this).

**P2's residual limit, documented (not a new defect, not fixed further this
pass):** `_claim_work` (`src/payments/dynamodb.py:1048`) still caps its
`WorkIndex` pagination at `for _ in range(20)` pages of up to
`min(max(limit*4,1),100)` items each — up to roughly 2,000 items examined per
claim call. This is a deliberate runaway guard, not an expected ceiling
(finish_*/mark_*/fail_* already remove an item's `GSI3PK`/`GSI3SK` once it
leaves a claimable state, so a real backlog shouldn't reach it), but it is a
real, bounded limit: a business that somehow accumulates more than ~2,000
stale-but-still-`GSI3`-indexed items ahead of genuinely claimable work in the
same `kind` partition could still see a claim call return fewer items than
requested, or none, without an error. Left as-is this pass — no reproduction
was attempted and no fix is proposed, since doing so responsibly would need
evidence this ceiling is ever actually approached in practice, not a
speculative increase.

## Fourth pass (supervision found the third pass's own fix was incomplete, plus a second, independent refund-idempotency defect)

Supervision reviewing the third pass's R28 fix found it was itself
incomplete: `_apply_existing_payment_snapshot` correctly stopped treating a
`provider_adjustment_id` as immutable, but it kept using the adjustment's
*reported* `status` as the proxy for whether money had already moved.
Reproduced exactly as described: a 200 partial adjustment against a 500
allocation observed `approved` (restores 200 → allocated 300) → `rejected`
(correctly no clawback, R28's contradiction handling) → `approved` again —
the second `approved` re-read as "not previously effective" (because
`status` was `"rejected"`, not in the effective set) and restored another
200, doubling the compensation to 400 against an allocation that only ever
had 200 to give. A second, quieter bug rode along with it: the payment's
`review_reason` (set to `"adjustment_effective_reversed"` by the middle,
contradictory observation) was silently cleared by the third, unrelated
`approved` observation, because the payment-level `Update` unconditionally
overwrote `review_reason` with whatever *this* call's own top-level
`assess_payment` found — usually nothing — instead of preserving an
already-open, unresolved review.

R31 below is the fix: `status` is no longer the source of truth for whether
the compensating balance mutation has happened. A separate, one-way marker —
`effect_applied_at`/`effect_applied_minor` — starts unset and, once set by
the *first* transition into an effective status, is never reset and never
reapplied by any later observation of the same adjustment id, no matter how
many more times the reported status swings between effective and
non-effective. The reported amount (`amount_minor`, free to be corrected by
the provider later) and the applied amount (`effect_applied_minor`, fixed
forever once set) are now tracked as two separate fields for exactly this
reason. The open-review fix is a companion, one-line change in the same
method: the payment's `review_reason` is now preserved across an unrelated
observation and only cleared by `resolve_review`'s explicit "acknowledge"
action — never silently overwritten back to empty just because the latest
snapshot itself found nothing new wrong.

Independently, supervision also found a second, unrelated defect in
`create_refund_operation`'s conflict handling (R32): two concurrent requests
sharing the SAME `payment_id` **and** the SAME Idempotency-Key can make both
the REFUND row (`TransactItems` index 0) and the REFUND_LOCK row (index 1)
fail their `ConditionalCheckFailed` at once — not just the lock. The
previous fix (R29) inspected only the lock's `CancellationReasons` entry, so
it misreported this exact case as `"another refund operation is unresolved"`
even though it was really the same logical request, already satisfied by
the concurrent winner. R32's fix re-reads this operation's own row with
`ConsistentRead` after any conflict: if it now exists and its payload
matches, it returns the winner exactly (idempotent, not an error); if it
exists with a different payload, it is a genuine key-reuse error; only when
neither this operation's own row nor a matching one exists, and the lock
belongs to someone else, is `"unresolved"` still correct; anything else
falls through to the existing bounded retry.

This pass also: fixed a mislabeling in the test suite itself (R33) — the
test claiming to validate "real" concurrent DynamoDB behavior
(`test_concurrent_refund_requests_with_different_keys_manual_real_threads`)
was still decorated with `@mock_aws`, so `RUN_REAL_CONCURRENCY_TESTS=1` only
ever raced real Python threads against Moto's in-memory backend, never
actual DynamoDB — renamed to
`test_concurrent_refund_requests_with_different_keys_moto_thread_stress` with
docs saying exactly that, and a genuinely separate integration test
(`tests/integration/test_real_dynamodb_refund_concurrency.py`, no
`@mock_aws` anywhere in the file) was added with its own double opt-in
(`RUN_REAL_AWS_CONCURRENCY_TESTS=1` plus an explicit
`REAL_DYNAMODB_TABLE_NAME`, no default) for manual validation against a real
deployed table after a real deploy. It was authored and reviewed but **not
executed** this pass — no AWS deploy exists yet to run it against, and doing
so was explicitly out of scope.

Finally, R30 (P7's GSI eventual-consistency mitigation from the third pass)
is upgraded from a mitigation to an actual resolution (R34): every real
caller of `mark_attempt_ready`/`mark_attempt_unknown` already holds the
attempt's own `business_id` (on the `PaymentAttempt` it just created or
fetched moments earlier in the same request), so both methods now take
`business_id` and resolve the attempt with a direct, strongly consistent
PK/SK `GET` on the base table instead of a `LookupIndex` (GSI) query — a GSI
is never eligible for `ConsistentRead`, which was the actual source of the
transient-miss risk the short retry only papered over. The retry helper
(`_attempt_by_id_settled`) is removed entirely; there is nothing left to
retry against a direct, already-consistent read.

**Both financial defects found this pass (R31, R32) are now fixed and
covered by tests that were first confirmed failing against the pre-fix
code.** That does not retroactively close the standing caution below: this
is the fourth time in four passes that review has found a real defect in
`dynamodb.py`'s payment/adjustment/refund/attempt machinery, and the third
time specifically in `_apply_existing_payment_snapshot` or its immediate
neighbors. Treat that as a strong signal that the *class* of risk (a state
transition modeled by a mutable field instead of a one-way marker, a
concurrent-write race, a paginated-index edge case) is not exhausted by
what has been found so far, not as evidence that four passes were enough to
find everything.

## Legend

- **Estado**: `resuelto` (fixed + tested), `pendiente` (found, not fixed this pass), `dependencia externa` (blocks on something outside this repo).
- Evidence paths are relative to `backend/` or `staff-workspace/` unless stated otherwise.

## A. Resuelto — correcciones críticas de dinero y ciclo de vida de pago

| ID | Severidad | Hallazgo | Archivo/función | Corrección | Prueba |
| --- | --- | --- | --- | --- | --- |
| R1 | Crítico | Tras un reembolso/reversión efectivo, el intento de Checkout Pro anterior seguía en estado `ready` y `get_or_create_attempt` lo reutilizaba, devolviendo al cliente un `checkout_url` obsoleto/ya usado en vez de permitirle pagar de nuevo. | `src/payments/dynamodb.py:_apply_existing_payment_snapshot` | Al aplicar un ajuste efectivo que reabre saldo, el intento asociado (`payment_attempt_id`) se transiciona a `expiring` dentro de la MISMA transacción que restaura el balance (espejo del comportamiento ya existente en `postgres.py`, nunca portado a DynamoDB). El worker de reconciliación ya sabía expirar `expiring`→`expired` y liberar el guard `ACTIVE_ATTEMPT`; solo faltaba esta transición inicial. | `tests/integration/test_dynamodb_payment_flow.py::test_effective_refund_expires_the_stale_attempt_and_a_clean_checkout_follows` (nuevo, cubre pago→reembolso→saldo restaurado→intento marcado→sin ventana de dos intentos activos→worker expira→checkout nuevo distinto→tercera llamada reutiliza el nuevo, no crea un cuarto). |
| R2 | ~~Crítico~~ **Superseded — see R21** | `_claim_work` sobrescribía siempre el `status`/`processing_status` devuelto a `"processing"` antes de regresarlo al llamador. | `src/payments/dynamodb.py:_claim_work` | **Esta fila documenta el estado de la primera pasada, que una revisión independiente encontró incompleto.** La primera pasada solo corrigió lo que se devolvía al llamador, no lo que se persistía — `_claim_work` seguía escribiendo `status="processing"` en DynamoDB durante el lease, rompiendo `mark_attempt_ready` y dejando ítems invisibles para siempre tras un solo reclamo. Ver R21 para la corrección real. | — |
| R3 | ~~Crítico~~ **Superseded — see R21** | `mark_attempt_expired` verificaba `status == "expiring"` releído de la base. | `src/payments/dynamodb.py:mark_attempt_expired` | Esta corrección seguía siendo necesaria (verificar `lease_token`, no el estado), pero dependía de la premisa incorrecta de R2; ver R21 para la corrección completa y consistente. | — |

**Limitación explícita de Moto reconocida**: estas pruebas validan la lógica de transición de estado y las condiciones exactamente como el código real las expresa (mismas `ConditionExpression`, mismo `transact_write_items`), pero Moto ejecuta las transacciones de forma secuencial/no realmente concurrente para la mayoría de las llamadas. Para la única prueba que sí usa hilos reales (`ThreadPoolExecutor`, ver R24), Moto exhibe una falla intermitente propia y ya conocida (no introducida por este trabajo): `RuntimeError: dictionary changed size during iteration` dentro de `copy.deepcopy` al copiar el estado interno de la tabla durante `TransactWriteItems` concurrentes. Se observó en aproximadamente 1 de cada 5 corridas completas de la suite. Es una limitación de la implementación en memoria de Moto bajo hilos reales, no una falla del código de producción bajo prueba — la misma clase de prueba (`test_concurrent_checkout_requests_share_the_single_active_attempt`) ya existía antes de esta auditoría con el mismo riesgo. No se intentó enmascarar con reintentos; queda documentado en vez de ocultado.

## Segunda pasada — resuelto

| ID | Severidad | Hallazgo | Archivo/función | Corrección | Prueba |
| --- | --- | --- | --- | --- | --- |
| R21 | Crítico | Corrección real de R2/R3: `_claim_work` seguía escribiendo `status`/`processing_status` a `"processing"` en el ítem persistido durante el lease, aunque ya devolviera el estado previo al llamador. Esto rompía `mark_attempt_ready` (que exige `status` en `"creating"/"unknown"/"ready"` releyendo fresco de la base) y dejaba ítems atascados para siempre en `"processing"` tras un solo reclamo, invisibles a reclamos futuros porque `"processing"` no está en la tupla de estados reclamables. Reproducido con Moto: un intento `unknown` fallaba su recuperación; un intento `ready` se reclamaba una vez y nunca más. | `src/payments/dynamodb.py:_claim_work` | El reclamo ya nunca toca `status`/`processing_status`; `lease_token`/`lease_expires_at` son el único mecanismo de exclusión (igual que todo `finish_*` en este archivo ya asumía). La `ConditionExpression` del reclamo ahora verifica también `#state IN (...)` para cerrar una carrera más estrecha que el diseño original tampoco cubría. Aplicado uniformemente a `attempt`/`refund`/`outbox`/`provider_event`, sin tratar reconciliación como caso especial. | `tests/integration/test_dynamodb_payment_flow.py::test_reconciliation_recovers_an_unknown_attempt_to_ready_without_getting_stuck`, `::test_reconciliation_of_a_ready_attempt_preserves_ready_and_is_reclaimable_after_next_at`, `::test_reconciliation_lease_prevents_a_concurrent_second_claim`, `::test_no_terminal_reconciliation_path_leaves_an_attempt_stuck_at_processing` (todas nuevas, contra `DynamoRepository` real con Moto, no un *fake*). |
| R22 | — | Comentarios y firma desactualizados dejados por la primera pasada (`mark_attempt_expired` describía la premisa incorrecta de R2; `run_reconciliation_worker` llamaba `finish_reconciliation` incluso tras `mark_attempt_expired`, liberando el mismo lease dos veces). | `src/payments/dynamodb.py:mark_attempt_expired`, `src/payments/workers.py:run_reconciliation_worker` | Comentarios corregidos para reflejar el diseño real (status nunca se sobrescribe); `run_reconciliation_worker` ya no llama `finish_reconciliation` en la rama terminal `expiring`→`expired`. | Mismas pruebas de R21. |
| R23 | Crítico (P1 de la primera pasada) | `_by_id` hacía `Scan` de toda la tabla con `Limit=100` para `payment`/`outbox`/`provider_event`/`refund` — podía devolver `None` para ítems reales una vez superados ~100 ítems totales en la tabla. | `src/payments/dynamodb.py:_by_id` | Migración completa (no parcial): se agregó `GSI1PK`/`GSI1SK` a `payment` y `outbox` en su único punto de creación; se reutilizó el de `refund` (ya existía, sin usar); se repropuso el de `provider_event` de `EVENT#{connectionId}#{hash}` (confirmado sin uso vía `_lookup` en ningún lugar) a `EVENT#{id}`. `_by_id` es ahora una consulta de una línea contra `LookupIndex`. | `tests/integration/test_dynamodb_payment_flow.py::test_by_id_lookups_resolve_correctly_with_over_100_irrelevant_items_in_the_table` (130 cargos de relleno antes del objetivo; ejercita las 4 rutas: `create_refund_operation`, `finish_refund_operation`, `finish_outbox`, `fail_provider_event`). |
| R24 | Crítico (P3 de la primera pasada) | `create_refund_operation`'s verificación de "sin otro reembolso pendiente" era una lectura simple *fuera* de la transacción (TOCTOU): dos solicitudes concurrentes con distinta `Idempotency-Key` para el mismo pago podían ambas crear una operación de reembolso real. | `src/payments/dynamodb.py:create_refund_operation`, `finish_refund_operation`, `resolve_review` | Ítem `REFUND_LOCK#{paymentId}` creado transaccionalmente junto con el reembolso (`attribute_not_exists(PK)`: solo un llamador puede ganar). La misma `Idempotency-Key` sigue siendo idempotente (su verificación de coincidencia exacta corre antes de tocar el lock). El lock se libera solo en estados terminales: `"completed"` (worker) y `"resolved"` (`resolve_review` acknowledge); `"review"` y `"retry"` lo conservan. | `tests/unit/test_dynamodb_repository.py::test_concurrent_refund_requests_with_different_keys_leave_only_one_active` (carrera real con `ThreadPoolExecutor` — ver limitación de Moto arriba), `::test_refund_review_retry_keeps_the_lock_but_acknowledge_releases_it`, `::test_refund_review_acknowledge_releases_the_lock_for_a_fresh_refund`. |
| R25 | Crítico (P4 de la primera pasada) | La cancelación de un cobro no tenía representación en el dominio; la única protección contra asignar dinero a un cobro cancelado era una `ConditionExpression` cruda dentro de la transacción de `record_payment_observation`. Si fallaba, toda la transacción se revertía y el error se relanzaba como reintentable para siempre — dinero real confirmado podía quedar huérfano, invisible, reintentando sin fin. | `src/payments/models.py:Charge`, `src/payments/service.py:assess_payment`, `src/payments/dynamodb.py:record_payment_observation` | `Charge` ahora expone `cancelled` (poblado desde `cancelled_at`); `assess_payment` devuelve `PaymentAssessment(False, "charge_cancelled")` cuando el cobro ya estaba cancelado al momento de evaluar — ninguna transacción condenada se intenta. Para la carrera real (cancelación concurrente entre la evaluación y el commit), `record_payment_observation` distingue esa causa específica de fallo y comité inmediatamente una transacción reducida (pago + auditoría + outbox, sin asignación) con `review_reason="charge_cancelled_after_payment"`, en vez de perder la evidencia o reintentar indefinidamente. | `tests/unit/test_dynamodb_repository.py::test_payment_for_an_already_cancelled_charge_is_recorded_without_allocation`, `::test_payment_confirmed_concurrently_with_cancellation_is_recorded_not_lost_or_retried_forever` (simula la carrera real: evalúa con un snapshot no cancelado, cancela, luego observa). |
| R26 | Alto (P6 de la primera pasada) | `_provider_number` exigía `isinstance(amount_minor, int)`, pero `claim_refund_operations` devuelve `amount_minor` tal como `boto3.resource` lo deserializa — `Decimal`, nunca `int`. Un reembolso parcial real (monto explícito) fallaba siempre contra el repositorio real, enmascarado porque las pruebas existentes solo usaban reembolso completo o literales `int`. | `src/payments/dynamodb.py:claim_refund_operations`, `src/payments/mercadopago.py:_provider_number` | `claim_refund_operations` castea `amount_minor` a `int` en el mismo lugar donde toda otra lectura en el archivo ya normaliza tipos de DynamoDB; `_provider_number` acepta defensivamente un `Decimal` de número entero (rechazando explícitamente cualquier fracción). | `tests/unit/test_mercadopago.py::test_refund_accepts_a_whole_number_decimal_amount_but_rejects_a_fraction`; `tests/integration/test_dynamodb_payment_flow.py::test_partial_refund_amount_from_a_real_claim_reaches_the_provider_correctly` (reclamo real vía Moto alimentando al `MercadoPagoSandbox` real, no un *fake* que nunca toca `_provider_number`). |
| R27 | Crítico (nuevo, no listado en la primera pasada) | Un solo snapshot del proveedor con **dos** ajustes nuevos efectivos a la vez (p. ej. un reembolso parcial y un chargeback observados juntos) hacía que `_apply_existing_payment_snapshot` intentara dos operaciones `Update` sobre el mismo ítem `Charge` dentro de una sola `TransactWriteItems` — DynamoDB lo rechaza (`ValidationException: Transaction request cannot include multiple operations on one item`), una falla determinística y permanente para ese pago. Independientemente del rechazo, la lógica también habría podido restaurar más saldo del que realmente estaba asignado, porque cada ajuste se acotaba contra la asignación original sin actualizar, no contra lo que quedaba tras los ajustes previos del mismo bucle. | `src/payments/dynamodb.py:_apply_existing_payment_snapshot` | Reproducido primero (prueba que falla con `ValidationException` antes de la corrección). Ahora acumula el restauro de cada ajuste nuevo efectivo en un total único, acotando cada uno contra un contador de "asignación restante" que decrece con cada ajuste del mismo bucle, y emite como máximo un `Update` sobre el `Charge` y como máximo uno sobre el `Attempt`, después del bucle. | `tests/unit/test_dynamodb_repository.py::test_two_new_effective_adjustments_in_one_snapshot_are_applied_correctly`, `::test_two_new_adjustments_whose_amounts_exceed_the_allocation_are_capped_not_summed_raw` (dos ajustes de 400+400 contra una asignación de 500: el segundo se acota a 100, no a su monto nominal). |

## Tercera pasada — resuelto

| ID | Severidad | Hallazgo | Archivo/función | Corrección | Prueba |
| --- | --- | --- | --- | --- | --- |
| R28 | Crítico — **fila superseded, ver R31**: esta corrección resultó a su vez incompleta; seguía usando `status` como indicador de si el dinero ya se había movido, lo que permitía una doble aplicación en approved→rejected→approved. R31 es la corrección completa. (encontrado por una segunda revisión independiente, sobre el propio código de R27) | `if self._get(business_id, sk): continue` trataba un `provider_adjustment_id`, y su `status`, como inmutables una vez visto. Reproducido con Moto antes de corregir: un ajuste observado primero como `"pending"` (correctamente no efectivo) nunca restauraba saldo aunque un snapshot posterior reportara el MISMO `provider_adjustment_id` como `"approved"` — el registro se quedaba en `"pending"` para siempre y el cobro seguía totalmente asignado, sin ningún error visible. `postgres.py` (`ON CONFLICT (business_id,payment_id,provider_adjustment_id) DO UPDATE SET status=EXCLUDED.status,...`) ya trata esto como un upsert mutable; la ruta DynamoDB nunca lo hizo. | `src/payments/dynamodb.py:_apply_existing_payment_snapshot` | Un ajuste ya existente ahora se actualiza, no se omite, salvo que la observación entrante sea estrictamente más vieja que la almacenada (comparando `provider_updated_at`/`provider_observed_at`, misma precedencia que ya usa el chequeo de vejez del pago a nivel superior). El dinero se mueve como máximo una vez por id de ajuste: solo una transición no-efectivo→efectivo suma al restauro; efectivo→efectivo (un "approved" duplicado, o una revisión de monto reportada por el proveedor) actualiza los campos guardados pero nunca vuelve a mover dinero; efectivo→no-efectivo ("approved"→"rejected") no tiene una regla automática segura — así que se conserva el efecto ya confirmado y la contradicción se marca con `review_reason="adjustment_effective_reversed"` en vez de adivinar (esta política de decisión se mantiene sin cambios en R31; lo que R31 corrige es que esta misma fila usaba `status` como indicador del efecto, permitiendo una doble aplicación — ver la sección "Cuarta pasada" para R31). Cada restauro se acota contra el `allocated_minor` **actual** del `Charge` (no el monto original de `Allocation`, congelado desde su creación), y la `Update` del ajuste lleva su propia condición de comparar-e-intercambiar (`status`+`provider_observed_at` esperados) para que dos observaciones concurrentes de la misma transición no restauren dos veces. | 7 pruebas nuevas en `tests/unit/test_dynamodb_repository.py`, todas confirmadas fallando contra el código sin corregir antes de implementar: `test_adjustment_pending_then_approved_restores_balance_exactly_once`, `test_duplicate_approved_adjustment_observation_is_idempotent`, `test_rejected_then_approved_restores_balance_exactly_once`, `test_older_snapshot_cannot_change_an_adjustment_already_seen_as_newer`, `test_concurrent_observations_making_the_same_adjustment_effective_restore_only_once` (simulación determinística vía `unittest.mock.patch`, no hilos reales — ver R29), `test_effective_partial_refund_then_effective_adjustment_never_exceeds_allocated_minor`, `test_transaction_touches_charge_and_attempt_at_most_once_each`, y `test_effective_adjustment_reversed_is_flagged_for_review_not_reverted` para la contradicción documentada. |
| R29 | — **la parte (b) de esta fila quedó superseded por R33** (la prueba determinista que reemplazó a la intermitente seguía sin ser una validación real de DynamoDB — ver R33); su parte (a), el reintento acotado, sigue vigente, pero el `create_refund_operation` que reintenta fue a su vez corregido por R32 para el caso de colisión de la misma Idempotency-Key. | Dos hallazgos operativos junto con R28: (a) P5 — `create_customer`, `create_charge` y `create_refund_operation` reintentaban un conflicto de condición recursándose sin límite; (b) la prueba de concurrencia de reembolsos de la segunda pasada (`ThreadPoolExecutor`) fallaba intermitentemente (~1 de cada 5 corridas) por una falla propia de Moto (`RuntimeError: dictionary changed size during iteration` en su `copy.deepcopy` interno bajo `TransactWriteItems` concurrentes) — documentarlo no bastaba, según se pidió explícitamente. | `src/payments/dynamodb.py` (las tres funciones), `tests/unit/test_dynamodb_repository.py` | (a) Cada función toma un contador privado `_attempt` (por defecto 1, no forma parte del contrato público), aplica backoff exponencial con *jitter* entre intentos, y se rinde con un `RuntimeError` claro tras 5 intentos en vez de seguir recursando. `create_refund_operation` además inspecciona `CancellationReasons` de la `TransactionCanceledException` para distinguir un conflicto genuino del `REFUND_LOCK` (no retryable: reintentar nunca cambia ese resultado) de un conflicto en la propia fila de idempotencia u otra causa transitoria (sí retryable, acotado). (b) La prueba con hilos reales se reemplazó como la prueba principal por una versión determinística que aplica un *patch* a la única lectura de "camino rápido" que un llamador concurrente real también podría ganar por una carrera, forzando a ambas llamadas a llegar de verdad a `transact_write_items`, y afirma que es la condición transaccional (no el camino rápido ni el tiempo de los hilos) la que rechaza a la segunda. La versión con hilos reales se conserva, renombrada y omitida por defecto (`RUN_REAL_CONCURRENCY_TESTS=1` para ejecutarla), para verificación manual contra DynamoDB real después de un despliegue. | `test_create_customer_conflict_retry_is_bounded_not_infinite`, `test_create_refund_operation_non_lock_conflict_retry_is_bounded` (confirman que el límite realmente termina, no solo que existe). `test_two_different_refund_keys_racing_the_same_payment_only_one_wins` (determinística, confirmada estable en 8 corridas seguidas, 0 fallas) reemplaza a `test_concurrent_refund_requests_with_different_keys_leave_only_one_active` como prueba de CI; esta última sigue existiendo como `test_concurrent_refund_requests_with_different_keys_manual_real_threads`, con `@pytest.mark.skipif` por defecto. |
| R30 | Medio (P7 de la primera pasada) — **mitigación superseded por R34**, que elimina por completo la dependencia del GSI en vez de solo reintentar contra ella | `mark_attempt_ready`/`mark_attempt_unknown` leen el intento recién creado vía `LookupIndex` (un GSI, sin `ConsistentRead`), típicamente milisegundos después de que la propia transacción de `get_or_create_attempt` lo creó en la misma solicitud. Una ausencia ahí no es tan concluyente como una ausencia en la tabla base — podía ser solo un retraso de propagación — pero el código la trataba igual que "este intento se completó con otra preferencia", devolviendo un 503 transitorio a un comprador cuyo checkout en realidad estaba bien. | `src/payments/dynamodb.py:_attempt_by_id_settled` (nuevo), `mark_attempt_ready`, `mark_attempt_unknown` | Hasta 2 reintentos cortos (50ms) antes de rendirse exactamente como antes. Acotado solo a estos dos llamadores, que leen el mismo intento dentro de la solicitud que probablemente lo creó; una búsqueda por id desde un pago ya almacenado (reconciliación, etc.) ya tuvo tiempo de sobra para asentarse y no lo necesita. | `test_mark_attempt_ready_tolerates_a_transient_gsi_lookup_miss` (dos ausencias simuladas y luego un acierto: ahora tiene éxito), `test_mark_attempt_ready_fails_promptly_not_in_a_loop_when_genuinely_missing` (una ausencia genuina falla con exactamente 3 lecturas totales, no en bucle) — **ambas pruebas fueron eliminadas en R34**, porque validaban el mecanismo de reintento contra el GSI que R34 elimina por completo; sus reemplazos (`test_mark_attempt_ready_never_consults_the_gsi_at_all`, `test_mark_attempt_ready_fails_promptly_with_exactly_one_read_when_genuinely_missing`) están en la fila de R34. |

## Cuarta pasada — resuelto

| ID | Severidad | Hallazgo | Archivo/función | Corrección | Prueba |
| --- | --- | --- | --- | --- | --- |
| R31 | Crítico (la propia corrección de R28 resultó incompleta, encontrado por supervisión) | `_apply_existing_payment_snapshot` seguía usando el `status` reportado como el indicador de si el dinero ya se había movido. Reproducido exactamente como se describió: un ajuste parcial de 200 sobre una asignación de 500, observado `approved` (restaura 200 → allocated 300) → `rejected` (correctamente sin reversión) → `approved` de nuevo — la segunda aprobación se leía como "nunca antes efectiva" (porque `status` era `"rejected"`) y restauraba otros 200, duplicando la compensación a 400. Además, `review_reason="adjustment_effective_reversed"` se borraba silenciosamente en la tercera observación, porque el `Update` del pago sobrescribía siempre `review_reason` con el resultado de la evaluación de *esa* llamada. | `src/payments/dynamodb.py:_apply_existing_payment_snapshot` | El efecto financiero ya no se infiere de `status` (libre de evolucionar approved↔rejected indefinidamente): un marcador separado y de un solo sentido, `effect_applied_at`/`effect_applied_minor`, se fija una sola vez en la primera transición a un estado efectivo y nunca se reinicia ni se vuelve a aplicar en ninguna observación posterior del mismo id de ajuste. El monto reportado (`amount_minor`, puede corregirse) y el monto realmente aplicado (`effect_applied_minor`, fijo para siempre una vez fijado) se guardan por separado. `review_reason` ya no se sobrescribe a vacío por una observación no relacionada — permanece hasta que `resolve_review` lo reconoce explícitamente ("acknowledge"). | 6 pruebas nuevas en `tests/unit/test_dynamodb_repository.py`, confirmadas fallando contra el código sin corregir antes de implementar: `test_approved_rejected_approved_never_applies_money_twice` (la reproducción exacta), `test_confirmed_pending_completed_never_applies_money_twice`, `test_contradiction_review_stays_open_until_explicit_resolution`, `test_concurrent_observations_racing_the_reject_then_approve_transition_cannot_double_apply`, `test_reported_amount_can_be_corrected_after_effect_applied_without_changing_applied_amount`; más 2 pruebas preexistentes de R27 actualizadas para verificar `effectAppliedMinor` (el monto aplicado, acotado) por separado de `amountMinor` (el monto reportado, sin acotar). |
| R32 | Crítico (defecto independiente de R31, encontrado por supervisión) | Dos solicitudes concurrentes con el MISMO `payment_id` y la MISMA Idempotency-Key podían hacer que tanto la fila REFUND (índice 0 de la transacción) como la fila REFUND_LOCK (índice 1) fallaran su `ConditionalCheckFailed` a la vez. La corrección anterior (R29) solo inspeccionaba la razón del índice 1 y devolvía incorrectamente "another refund operation is unresolved" — cuando en realidad era la misma solicitud lógica, ya satisfecha por el ganador concurrente. | `src/payments/dynamodb.py:create_refund_operation` | Tras cualquier conflicto, se relee con `ConsistentRead` la fila propia (por `operation_key`): si existe y el monto/`full_refund` coincide, se devuelve el ganador exactamente (idempotente, no un error); si existe con datos distintos, es un error genuino de reutilización de clave; solo si la fila propia no existe y el lock pertenece a otra operación se devuelve "unresolved"; cualquier otro caso cae al reintento acotado ya existente (causas verdaderamente transitorias). | `test_same_idempotency_key_racing_itself_returns_the_winning_refund_not_unresolved` (determinista: fuerza una `TransactionCanceledException` real con `ConditionalCheckFailed` en AMBOS índices 0 y 1, capturada y verificada explícitamente, con la lectura inicial de este llamador simulada como desactualizada; confirmada fallando contra el código sin corregir, devolviendo el ganador exacto tras el fix). Ajustada también `test_two_different_refund_keys_racing_the_same_payment_only_one_wins` (su *fake* del camino rápido ahora solo engaña las dos lecturas iniciales, no la relectura de recuperación del fix). |
| R33 | Medio (higiene de pruebas) | `test_concurrent_refund_requests_with_different_keys_manual_real_threads` afirmaba validar concurrencia real de DynamoDB pero seguía decorada con `@mock_aws` — `RUN_REAL_CONCURRENCY_TESTS=1` solo corría hilos reales contra el backend en memoria de Moto, nunca DynamoDB real. | `backend/tests/unit/test_dynamodb_repository.py`, `backend/tests/integration/test_real_dynamodb_refund_concurrency.py` (nuevo) | Renombrada a `test_concurrent_refund_requests_with_different_keys_moto_thread_stress`, con documentación honesta de que sigue siendo una prueba de estrés de Moto, no de DynamoDB real. Se creó un archivo de integración separado, sin `@mock_aws` en absoluto, con su propio doble opt-in (`RUN_REAL_AWS_CONCURRENCY_TESTS=1` + `REAL_DYNAMODB_TABLE_NAME` explícito, sin valor por defecto) para validación manual contra una tabla real desplegada. | Verificado que el nuevo archivo colecciona y se omite (`SKIPPED`) sin ningún acceso a AWS cuando el doble opt-in no está presente (confirmado en esta sesión). **No se ejecutó la versión contra AWS real** — no hay despliegue disponible y estaba explícitamente fuera de alcance esta pasada. |
| R34 | Medio (P7/R30 de la tercera pasada, mitigación elevada a resolución) | R30 (tercera pasada) redujo el riesgo de una lectura transitoriamente ausente en `LookupIndex` (un GSI, nunca elegible para `ConsistentRead`) con un reintento corto, pero no lo eliminaba. | `src/payments/dynamodb.py:mark_attempt_ready,mark_attempt_unknown` (ahora reciben `business_id`); `src/payments/service.py`, `src/payments/workers.py`, `src/payments/postgres.py` (firmas actualizadas en el único llamador real de cada uno) | Todo llamador real ya tiene `business_id` disponible (en el propio `PaymentAttempt` que acaba de crear o leer segundos antes en la misma solicitud). Ambos métodos ahora reciben `business_id` y resuelven el intento con un `GET` directo por PK/SK sobre la tabla base — fuertemente consistente — en vez de una consulta al GSI. Se eliminó por completo el helper de reintento (`_attempt_by_id_settled`): no queda nada que reintentar contra una lectura directa ya consistente. | `test_mark_attempt_ready_never_consults_the_gsi_at_all` (hace que `_lookup`, el camino del GSI, lance una excepción si se llama; `mark_attempt_ready` debe seguir funcionando), `test_mark_attempt_ready_fails_promptly_with_exactly_one_read_when_genuinely_missing` (una ausencia genuina falla con exactamente 1 lectura, no reintentos). |

## B. Resuelto — página pública y frontend de staff

| ID | Severidad | Hallazgo | Archivo/función | Corrección | Prueba |
| --- | --- | --- | --- | --- | --- |
| R4 | Alto | Un enlace inválido, revocado, expirado o de un cobro cancelado devolvía JSON crudo (`{"error":"charge_not_found"}`) a un navegador que pidió `text/html`, rompiendo la marca justo en el primer contacto. Todas estas causas compartían la misma excepción (`InvalidChargeLink`), así que ya no podían distinguirse externamente — lo cual es correcto para no revelar por qué un enlace no funciona. | `src/payments/transport.py:handle_public`, `src/payments/transport.py:_error_html` (nueva) | Cuando el navegador pide HTML, se renderiza una página de error genérica y de marca (mismo CSP/no-store/nosniff que la página normal), sin datos del cobro. JSON sin cambios para consumidores de API. Igual tratamiento para el 503 "temporalmente no disponible". | `tests/unit/test_payment_transport.py::test_invalid_link_returns_json_for_api_callers`, `::test_invalid_revoked_expired_or_cancelled_links_render_the_same_generic_branded_page` (nuevas). |
| R5 | Alto | La clave de idempotencia del navegador para iniciar checkout usaba una sola llave fija de `sessionStorage` (`'charge-attempt-key'`), compartida entre cobros distintos abiertos en la misma pestaña/sesión. El backend no se ve comprometido financieramente (el guard es por `charge_id`, no por la clave), pero el contrato de idempotencia por cobro se rompía y el registro de auditoría podía mostrar la misma clave para cobros no relacionados. | `src/payments/transport.py` (script embebido en `_charge_html`) | La clave ahora se namespacea por `location.pathname` (que incluye el token del cobro), aislándola por cobro automáticamente. | Verificado manualmente (no hay arnés de pruebas de navegador); `tests/unit/test_payment_transport.py` sigue verde tras el cambio (no dependía del literal anterior). |
| R6 | Alto | `WorkspaceShell` no leía `loading` de `useAuth()`; una recarga o navegación directa a una URL profunda mostraba "Sin acceso" mientras `restoreSession()` aún resolvía, incluso con sesión válida. | `staff-workspace/src/components/workspace-shell.tsx` | Se destructura `loading` y se muestra `LoadingState` antes de evaluar `membership`. | `npm run typecheck`/`npm run build` verdes; no hay arnés de pruebas de componentes en este proyecto (ver limitaciones). |
| R7 | Alto | Un 401 (token expirado) no se distinguía de otros errores; cada pantalla mostraba "revisa tu internet" y reintentaba contra el mismo token inválido indefinidamente, sin ofrecer volver a iniciar sesión. | `staff-workspace/src/components/states.tsx`, `src/auth/auth-adapter.ts`, `src/auth/auth-context.tsx` | Nuevo `SessionExpiredState` (para `status===401`) que limpia el token local vía un nuevo `clearSession()` (distinto de `signOut()`: no dispara el redirect de logout del IdP) y ofrece un botón "Iniciar sesión de nuevo" que llama `signIn()`. No se implementó refresh de token (no hay flujo de refresh seguro disponible), tal como se pidió explícitamente. | `npm run typecheck`/build verdes. |
| R8 | Alto | Las claves de idempotencia de "Cancelar cobro" y "Solicitar reembolso" se regeneraban en cada clic/reintento (`newKey(...)` sin persistir), a diferencia del patrón ya correcto en `create-charge-page.tsx`. Un reintento tras una respuesta perdida enviaba una clave distinta. | `staff-workspace/src/pages/charge-detail-page.tsx` | Se usa `getOrCreateOperation("cancel-charge", chargeId, ...)` / `getOrCreateOperation("refund", paymentId, ...)`, limpiando la clave solo en `onSuccess`. | `npm run typecheck`/build verdes. |
| R9 | Medio | `SettingsPage` no manejaba `query.isError`; un fallo transitorio se renderizaba como "Sin configurar", pudiendo hacer creer que se perdió la conexión con Mercado Pago. | `staff-workspace/src/pages/settings-page.tsx` | Se añade el mismo patrón `if (query.isError) return <ErrorState .../>` usado en el resto de pantallas. | `npm run typecheck`/build verdes. |
| R10 | Medio | `staff_api_not_configured` (build de producción sin `VITE_STAFF_API_BASE_URL`) usaba el mismo código de estado (0) y el mismo mensaje que un error de red real ("revisa tu internet"), ocultando que era un problema de despliegue. | `staff-workspace/src/components/states.tsx` | Rama explícita por `code==="staff_api_not_configured"` con mensaje distinto ("Aplicación no configurada... contacta a soporte técnico"). | `npm run typecheck`/build verdes. |
| R11 | Medio | Falta `nonce` en el flujo OIDC (solo `state`+PKCE existían); el `id_token` recibido no se validaba contra ningún valor atado a la solicitud de autorización. | `staff-workspace/src/auth/auth-adapter.ts` | Se genera y envía `nonce` junto con `state`/`verifier`; `exchangeCallback` decodifica el `id_token` devuelto y rechaza la sesión si el `nonce` no coincide con el guardado. | `npm run typecheck`/build verdes (sin arnés de pruebas de este flujo — ver limitaciones). |

## C. Resuelto — totales/paginación, GitHub Free y seguridad de infraestructura

| ID | Severidad | Hallazgo | Archivo/función | Corrección | Prueba |
| --- | --- | --- | --- | --- | --- |
| R12 | Alto | La pantalla "Hoy" calculaba "Pendientes" y "Saldo por cobrar" sobre `listCharges` (tope de 100, ordenado por creación), omitiendo silenciosamente cobros abiertos más antiguos —justo los más propensos a estar vencidos— sin ningún aviso de que la cifra era parcial. | `staff-workspace/src/pages/today-page.tsx`; backend: `src/payments/dynamodb.py:business_summary` (nuevo), `src/payments/staff.py`, `src/payments/staff_transport.py`, `backend/template.yaml` (ruta `GET /businesses/{businessId}/summary`) | Se implementó la solución preferida: un read model agregado dedicado que recorre **todos** los cargos abiertos del negocio (paginación interna completa de `_items`, sin el tope de 100 de la lista de exhibición) y devuelve `{openChargeCount, outstandingMinor}`. La pantalla "Hoy" ahora usa ese endpoint en vez de `listCharges`. | Backend: `tests/unit/test_dynamodb_repository.py::test_business_summary_aggregates_every_open_charge_beyond_the_list_page_limit` (120 cargos: `listCharges` devuelve 100, `business_summary` cuenta 120/$1,200.00 correctamente), más el caso de exclusión de cancelados. `tests/unit/test_staff_flow.py::test_business_summary_is_membership_checked_and_not_capped_like_the_list`, `::test_staff_transport_exposes_the_uncapped_business_summary_route`. Frontend: `npm run typecheck`/build verdes. |
| R13 | — | `deploy-pilot.yml` usaba `environment: pilot` y secretos de environment, incompatibles con GitHub Free en repo privado; interpolaba inputs/secrets directamente dentro de bloques `run:` (riesgo de inyección de shell); obtenía credenciales AWS antes de validar/compilar; no especificaba mecanismo de resolución de bucket S3 para artefactos. | `.github/workflows/deploy-pilot.yml` | Reescrito: sin `environment:`, lee secretos/variables a nivel repositorio; `sam validate --lint` + `sam build` ocurren ANTES de `configure-aws-credentials`; todos los inputs/secrets pasan por `env:` y se referencian como `"$VAR"` en el shell (nunca `${{ }}` dentro del cuerpo del script); `sam deploy` usa `--resolve-s3`. `concurrency` por `stack_name` sin cancelar despliegues en curso, ya presente, conservado. | `sam validate --lint` verde; validación de sintaxis YAML con PyYAML verde (actionlint no disponible en este entorno — ver limitaciones); revisión manual línea por línea de cada `${{ }}` restante (todas fuera de bloques `run:`, en `env:`/`with:`/`concurrency:`, donde no hay riesgo de inyección de shell). |
| R14 | Medio | `ses:SendEmail` concedido sobre `Resource: "*"` en vez de la identidad remitente configurada. | `backend/template.yaml:OutboxWorkerFunction` | `Resource` acotado a `arn:aws:ses:${AWS::Region}:${AWS::AccountId}:identity/${NotificationFromEmail}`. | `sam validate --lint` verde (cambio de plantilla, no desplegado). |
| R15 | Medio | `OperationalHealthFunction` (solo hace `Scan` de lectura) tenía `DynamoDBCrudPolicy` completo (lectura+escritura+borrado). | `backend/template.yaml:OperationalHealthFunction` | Cambiado a `DynamoDBReadPolicy` (plantilla gestionada de SAM, acotada a acciones de lectura sobre la tabla e índices). | `sam validate --lint` verde. |
| R16 | Medio | Sin retención explícita de CloudWatch Logs; los 9 grupos de logs de Lambda quedarían en "Never expire" por defecto. | `backend/template.yaml` | Se declaran 9 recursos `AWS::Logs::LogGroup` (uno por función) con `RetentionInDays: 30`, mismo nombre que el grupo autogenerado por Lambda para que CloudFormation los administre. Documentado como punto de partida para el piloto, no una decisión de cumplimiento. | `sam validate --lint` verde. |
| R17 | Bajo | Sin `.python-version` (backend) ni `engines.node` (staff-workspace) para detectar temprano el desvío entre el entorno local (Python 3.14 detectado en esta máquina) y Lambda/CI (Python 3.12) o Node local vs Node 22 de CI. | `backend/.python-version` (nuevo), `staff-workspace/package.json` | Añadidos ambos. No resuelve el desvío existente, solo lo hace visible a herramientas como pyenv/asdf y a `npm install` con `engine-strict`. | N/A (metadatos). |
| R18 | Medio | `boto3`/`botocore` se usan directamente en runtime (`dynamodb.py`, `secrets.py`) pero nunca se declaraban en `requirements.txt`; lo que CI/local prueban (vía la dependencia transitiva de `moto`) no estaba garantizado a coincidir con lo que Lambda ejecuta. | `backend/src/requirements.txt` | Se fija `boto3==1.43.18` explícitamente (versión ya validada localmente; sin extensiones nativas, compatible con arm64 sin build en contenedor). | `python -m pytest backend/tests/unit backend/tests/integration/test_dynamodb_payment_flow.py -q` verdes tras el cambio. |
| R19 | — | Ambigüedad de alcance: `docs/ARCHITECTURE.md` decía "a self-service refund initiation UI is deferred", pero el backend y el frontend de staff ya implementan una solicitud de reembolso completa, autenticada y solo para el propietario. | `docs/ARCHITECTURE.md` | Se aclaró que "self-service" se refiere al autoservicio de **clientes** (que sigue diferido junto con las cuentas de autoservicio), no a la acción de staff. Se declaró explícitamente que la solicitud de reembolso sandbox iniciada por el propietario **sí** está en el alcance del piloto, con sus casos de aceptación requeridos, y que permanece bloqueada para dinero real hasta que se resuelva el 401 de Mercado Pago (ver E1). | Revisión de texto; sin prueba automatizable. |
| R20 | — | Un cambio parcial no probado (`GSI1PK` de `capture_provider_event` cambiado de una identidad por conexión+evento a una identidad por `event_id`) quedó aplicado en el árbol de trabajo sin que el refactor más amplio de `_by_id` que lo motivaba se completara. | `backend/src/payments/dynamodb.py` | Revertido a su forma original (verificado con `git diff` vacío para ese archivo antes de aplicar las correcciones R1–R3). La migración completa a búsquedas por índice queda documentada como pendiente en P1, para hacerse de una sola vez y con pruebas, no por partes. | `git diff -- backend/src/payments/dynamodb.py` confirmado vacío antes de empezar R1. |

## C.1 Resuelto — quinta pasada de remediación local

| ID | Hallazgo resuelto | Corrección | Verificación |
| --- | --- | --- | --- |
| R35 (P8) | Clientes y revisiones se truncaban sin avisar. | El repositorio devuelve una página con `items`/`hasMore`; la API conserva `customers`/`reviews` y añade `hasMore`; Clientes, Revisión y el selector de nuevo cobro muestran cuando solo aparecen los 200 más recientes. | 47 pruebas dirigidas backend (1 omitida) y frontend typecheck/6 tests/build verdes. |
| R36 (P12) | Los fallos de workers no dejaban metadata estructurada y diagnosticable. | Los cuatro workers emiten JSON con worker, tipo de excepción, conteo e identificadores de negocio/operación. No serializan el mensaje de excepción, payload, correo, token ni respuesta del proveedor. | Nueva prueba fuerza un error que contiene un token señuelo y confirma que no aparece en el log. |
| R37 (P11, supersede R15) | Las Lambdas heredaban CRUD amplio sobre DynamoDB. | Cada función declara solo sus acciones reales; Public no puede `PutItem`, webhook no puede transaccionar, outbox solo puede leer/actualizar y salud solo puede `Scan` de la tabla base. | `sam validate --lint` válido; no queda ningún `DynamoDBCrudPolicy`/`DynamoDBReadPolicy`. |
| R38 (P9) | El adaptador PostgreSQL histórico se empaquetaba en todas las Lambdas. | Adaptador y suite histórica movidos a `backend/legacy/`; ningún `CodeUri: src/` los incluye. | 11 pruebas históricas se descubren y omiten limpiamente sin `TEST_DATABASE_URL`; `backend/src/payments/postgres.py` ya no existe. |
| R39 (P15) | El registro de dependencias trataba capacidades entregadas como trabajo futuro. | Registro reescrito contra el código actual, separando capacidades entregadas, cualificación pendiente y gates externos/de cuenta. | Revisión cruzada con rutas, plantilla, tests y audit log actuales. |

## D. Pendiente — hallazgos locales reales

Actualización de la quinta pasada: **P8, P9, P11, P12 y P15 están resueltos**
por R35–R39 y sus filas originales se conservan abajo únicamente como
trazabilidad histórica. Los pendientes locales que siguen abiertos son P10,
P13 y P14; P16 es metadata externa informativa.

P1, P2, P3, P4 y P6 de la primera pasada quedaron resueltos en la segunda
(ver R21–R27 arriba); P5 y P7 quedaron resueltos en la tercera (ver R29 y
R30 arriba). Se retiraron de esta tabla. Los números de hallazgo no se
reutilizan.

| ID | Severidad | Hallazgo | Archivo/función | Por qué no se corrigió ahora | Corrección recomendada |
| --- | --- | --- | --- | --- | --- |
| P8 | Medio | `list_customers` (tope 200) y `list_reviews` (tope 200) pueden omitir registros más antiguos sin ninguna señal de truncamiento — mismo patrón que R12, no corregido aquí. Para Reviews en particular, esto podría ocultar una revisión financiera abierta si un negocio acumula más de 200 revisiones históricas. | `src/payments/dynamodb.py:list_customers,list_reviews`; `staff-workspace/src/pages/customers-page.tsx`, `reviews-page.tsx` | Se priorizó la corrección completa para el caso de mayor visibilidad/riesgo de mala interpretación (el total del dashboard "Hoy", R12); extender el mismo patrón a estas dos listas cambia la forma de su respuesta y sus 4 puntos de consumo (contrato, cliente HTTP, fixture, página) — se dejó como seguimiento acotado en vez de apresurarlo. | Igual que R12: agregar un endpoint/summary equivalente para reviews (conteo real de revisiones abiertas), o como mínimo devolver `hasMore` desde el backend y mostrarlo en la UI. |
| P9 | Bajo | El código histórico de PostgreSQL (`backend/src/payments/postgres.py`, ~1300 líneas) sigue empaquetándose físicamente en cada Lambda desplegada (todas comparten `CodeUri: src/`), aunque nada del camino DynamoDB activo lo importa. | `backend/src/payments/postgres.py`, `backend/template.yaml` | Moverlo fuera de `backend/src/` es seguro en principio (solo un test de integración lo importa, ya excluido de CI por nombre de archivo explícito en `verify.yml`), pero se decidió no arriesgar romper esa referencia histórica sin tiempo para confirmarlo con calma. | Mover `postgres.py` (y `tests/integration/test_postgres_payment_flow.py`) fuera de `backend/src/`, p. ej. a `backend/legacy/`, ajustando el único import relativo. |
| P10 | Bajo | `work/local_sandbox_server.py` (herramienta de desarrollo local, no se despliega) todavía usa `PostgresRepository`/`LOCAL_DATABASE_URL`, inconsistente con la decisión de DynamoDB. | `work/local_sandbox_server.py` | Herramienta de scratch, bajo impacto real; no se tocó para no ampliar el alcance de esta auditoría a scripts exploratorios. | Migrar a `DynamoRepository` con una tabla local (Moto o DynamoDB Local) si esta herramienta sigue en uso, o eliminarla si ya no lo está. |
| P11 | Medio | Todas las funciones (excepto la ahora corregida `OperationalHealthFunction`) usan `DynamoDBCrudPolicy` sin acotar a las acciones que realmente ejecutan (p. ej. `PublicPaymentFunction`/`MercadoPagoWebhookFunction` no necesitan `dynamodb:DeleteItem` ni `BatchWriteItem`). | `backend/template.yaml` (todas las funciones salvo `OperationalHealthFunction`) | Requiere enumerar con precisión las acciones reales de cada handler contra `dynamodb.py` para no romper nada; se corrigió el caso más claro y de menor riesgo (una función 100% de lectura) como demostración del patrón. | Reemplazar `DynamoDBCrudPolicy` por declaraciones `Statement` específicas por función, basadas en un inventario de qué llamadas hace cada handler. |
| P12 | Medio | No hay logging estructurado en `backend/src`; los `except` de los workers solo guardan una cadena corta y truncada en DynamoDB, sin traceback, id de negocio/operación ni cuerpo de respuesta del proveedor. Diagnosticar un incidente real requeriría instrumentar y re-desplegar. | `src/payments/workers.py` (todos los `except`) | Cambio transversal (toca todos los puntos de manejo de error) que se priorizó por debajo de las correcciones de corrección financiera directa dado el tiempo disponible. | Añadir `logging` estructurado (stdlib, sin dependencia nueva) en cada `except`, con tipo de excepción, negocio/operación (nunca secretos/tokens) y el paso donde ocurrió. |
| P13 | Bajo | `sam build` sin contenedor falla localmente en esta máquina: Python local es 3.14, Lambda/CI usan 3.12, y Docker Desktop no está corriendo aquí para `--use-container`. Reproducido en vivo durante esta auditoría (no es especulación). | Entorno local únicamente | No corresponde arreglar el entorno local del auditor; `.python-version` (R17) es la mitigación disponible en el repo. | Instalar Python 3.12 localmente (pyenv/asdf) o iniciar Docker Desktop para build por contenedor cuando se necesite compilar localmente. La CI real ya usa Python 3.12 correctamente vía `actions/setup-python@v5` — este punto no afecta el pipeline de despliegue. |
| P14 | Bajo | `sam build`/`sam deploy` no usan `--use-container`; hoy es seguro porque ninguna dependencia declarada (`mercadopago`→`requests`, `boto3`) tiene extensiones nativas, pero nada en el pipeline lo garantiza para el futuro. | `.github/workflows/deploy-pilot.yml` | No se agregó `--use-container` porque forzaría descargar imágenes de contenedor en cada ejecución sin beneficio actual; se documenta la restricción explícitamente en su lugar. | Si se agrega una dependencia con extensiones nativas (p. ej. `cryptography`, `psycopg2-binary`), añadir `--use-container` en ese momento, no antes. |
| P15 | Bajo | `docs/delivery/2026-09-13-payment-platform-dependency-register.md` está desactualizado: lista como "Next" (P0) ítems como "Staff session bootstrap" y "Staff read models" que ya fueron entregados en commits posteriores (`753f4c3`, `ae390c7`, `86f1a11`, `dddcbf3`, etc.) sin que el registro se actualizara. | `docs/delivery/2026-09-13-payment-platform-dependency-register.md` | Corregirlo bien requiere un repaso completo del documento contra el código actual, no solo la tabla afectada; se dejó pendiente para no publicar una actualización parcial igualmente desactualizada. | Repasar cada fila contra el estado real del código y actualizar en una pasada dedicada. |
| P16 | Informativo | La descripción del repositorio en GitHub (metadato, no código) sigue anunciando alcance diferido ("Openpay + SAT-compliant CFDI (Facturapi)"), contradiciendo la arquitectura vigente. | Configuración del repositorio en GitHub (fuera del código) | No es código ni workflow/script/plantilla; cambiarlo no estaba autorizado explícitamente en esta pasada. | El dueño del repositorio puede actualizar la descripción desde GitHub Settings cuando lo considere oportuno. |

## E. Dependencia externa — no se puede resolver desde el código

| ID | Hallazgo | Evidencia | Bloquea |
| --- | --- | --- | --- |
| E1 | Mercado Pago sandbox devuelve HTTP 401 (`Unauthorized use of live credentials`) en el endpoint de reembolsos, incluso con credenciales de Test recién generadas y una conexión verificada como `test_user`. La operación quedó retenida en la cola de revisión; no se simuló ninguna reversión de saldo. | `docs/delivery/2026-09-11-canonical-payment-core-slice.md` (líneas 129–135), `work/pr_body.md`, `work/inspect_refund_failure.py` (script de diagnóstico existente, **no ejecutado** en esta auditoría — llamaría al proveedor real y leería un secreto real de Secrets Manager). | Cualificación de reembolsos completos y parciales de extremo a extremo; el botón de "Solicitar reembolso" del piloto (ver R19) permanece sin evidencia de éxito real hasta que esto se resuelva con Mercado Pago. |
| E2 | No existe (o no se pudo verificar) un rol IAM de AWS con OIDC configurado para que `deploy-pilot.yml` asuma credenciales. | No hay acceso de lectura a IAM de la cuenta AWS de destino desde esta sesión. | El primer despliegue no puede ejecutarse sin este rol y su trust policy (ver el runbook para la condición exacta recomendada). |
| E3 | No existen los secretos/variables a nivel de repositorio (`AWS_REGION`, `AWS_DEPLOY_ROLE_ARN`, `APPLICATION_SECRET_ARN`, `PROVIDER_SECRETS_ARN_PATTERN`). | `mcp__plugin_github_github__*` no expone verificación de secretos vía API de lectura disponible; confirmado por diseño (GitHub no expone valores de secretos, solo nombres a quien tiene permiso de administración). | El workflow fallará en el paso de `configure-aws-credentials`/`sam deploy` sin ellos. |
| E4 | `main` (rama por defecto) no tiene los workflows todavía — confirmado vía la API de GitHub: `head` de `codex/canonical-payment-slice` es `1bdf6bee...`, `base` de `main` es `ecb4a85a...`, y el PR #2 (`+10717/-6`) contiene prácticamente todo el backend/frontend, incluyendo `.github/workflows/`. GitHub solo dispara `workflow_dispatch` para un workflow presente en la rama por defecto (regla de la plataforma, no configuración de este repositorio). | `mcp__plugin_github_github__pull_request_read`, `list_branches` (llamadas de solo lectura realizadas en esta sesión). | Ningún dispatch de `deploy-pilot.yml` puede ejecutarse — ni desde la UI ni desde `gh`/API — hasta fusionar el PR #2 a `main`. |
| E5 | No confirmado: proveedor de identidad de staff (Cognito u otro) provisionado, tema SNS de alertas con suscriptor humano confirmado, remitente SES verificado. | Ninguna herramienta de esta sesión tiene acceso de lectura a esos recursos de AWS. | Los `workflow_dispatch` inputs que los requieren (`staff_auth_issuer`, `staff_auth_audience`, `alarm_topic_arn`, `notification_from_email`) no tienen valores reales todavía. |
| E6 | El plan exacto de GitHub (Free/Pro) de la cuenta personal `jorgecantu276` no se pudo confirmar por API (el repo es privado, tipo de owner `User`, confirmado; el campo de plan no está expuesto por las llamadas de solo lectura disponibles). | `mcp__plugin_github_github__search_repositories` (confirma `private:true`, `owner.type:User`). | No bloquea nada: el diseño del workflow (sin `environment:`, secretos a nivel repositorio, `workflow_dispatch` manual) funciona igual en Free o Pro. Se señala solo para que quede constancia de que no se asumió el plan, se diseñó para el peor caso (Free). |

## Archivos modificados (cinco pasadas)

Ver `git log --stat codex/canonical-payment-slice ^1bdf6be` para la lista exacta commit por commit, ninguno con `push`. En resumen: `backend/src/payments/{dynamodb,runtime,transport,staff,staff_transport,workers,mercadopago,models,service,api_gateway}.py`, `backend/legacy/{postgres,test_postgres_payment_flow}.py`, `backend/src/{public_payment,staff_api}/app.py`, `backend/template.yaml`, `backend/src/requirements.txt`, `backend/.python-version`, pruebas correspondientes en `backend/tests/` (incluyendo el nuevo `backend/tests/integration/test_real_dynamodb_refund_concurrency.py`), `.github/workflows/deploy-pilot.yml`, `docs/ARCHITECTURE.md`, los documentos de entrega, y `staff-workspace/src/{api,auth,components,fixtures,pages}/*`.

## Lo que esta auditoría NO verificó (dicho explícitamente)

- No se ejecutó ningún flujo en un navegador real ni con DynamoDB Local/AWS real; toda la verificación de concurrencia es por inspección del código + pruebas con Moto. La mayoría de esas pruebas ejecutan las llamadas secuencialmente (no hay carrera de red real); las que usan hilos reales (`ThreadPoolExecutor`, R24 y su renombrado en R33) exponen una falla intermitente propia de Moto bajo `TransactWriteItems` concurrentes (ver la nota de limitación en la sección A) — no es una falla del código de producción, pero tampoco es prueba de que Moto simule con exactitud la atomicidad real de DynamoDB bajo carga de red genuina. R33 añadió un archivo de integración separado, sin `@mock_aws`, para cerrar esa brecha contra una tabla real, pero **no se ejecutó** contra AWS real esta pasada — no hay despliegue disponible.
- No se instaló `pip-audit` ni se buscaron CVEs específicos por versión — se reportan las versiones instaladas exactas (`mercadopago==3.6.0`, `boto3==1.43.18`, `requests==2.33.1`) sin inventar hallazgos de seguridad no verificados. `npm audit --omit=dev --audit-level=high` en `staff-workspace` reportó 0 vulnerabilidades (verificado de nuevo al cierre de esta quinta pasada).
- No se llamó a Mercado Pago ni se leyó ningún secreto real de Secrets Manager.
- No se verificó el estado de ningún stack de AWS ya desplegado (no se tienen credenciales ni se buscaron).
- El escenario combinado de más de un ajuste nuevo efectivo con un intento todavía `ready` sí está cubierto por `test_transaction_touches_charge_and_attempt_at_most_once_each`: confirma un solo `Update` por ítem, saldo restaurado y transición del intento a `expiring`.
- P5, P7 (ahora R34, resuelto en vez de solo mitigado) quedaron corregidos en pasadas anteriores. P2's residual page/item cap in `_claim_work` (~2,000 items per claim call) remains a known, bounded limit, documented but not further reduced — see the third-pass note above.
- Ningún hallazgo de esta auditoría, en ninguna de las cinco pasadas, constituye una certificación de que no existan más defectos de la misma clase (condiciones de carrera, invariantes financieros, paginación, un estado modelado por un campo mutable en vez de un marcador de un solo sentido) en el resto de `dynamodb.py` que no fueron señalados explícitamente por revisión/supervisión independiente o por el análisis propio.
- **Preflight independiente de esta sesión (2026-09-14):** revisión línea por línea de los 5 commits de la quinta pasada (paginación P8, logging estructurado P12, permisos IAM por función P11, separación de PostgreSQL P9, reconciliación de documentación P15) contra el código real, trazando cada handler hasta las llamadas DynamoDB reales que ejecuta. No se encontró ningún defecto funcional, financiero o de seguridad nuevo — el único hallazgo fue la inconsistencia de numeración de pasadas/commits corregida en esta misma edición. Ver la nota de "riesgos reales" en el reporte de preflight de esta sesión (no versionado como archivo separado) para el hallazgo no bloqueante sobre `work/local_sandbox_server.py` (script ignorado por git, fuera del despliegue) que todavía importa `payments.postgres`, ruta que ya no existe tras R38.
