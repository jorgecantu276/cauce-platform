# ETL: retención, purga y apply controlado

Estado: entrega de la rama `codex/pilot-hardening`, sobre `c2d1701`. Nada de
esto está desplegado ni probado contra AWS real: toda la evidencia es Moto +
Vitest + un recorrido manual en navegador con fixtures. **Este documento no
autoriza datos reales** (ver §7).

Complementa `2026-09-14-import-jobs-api.md` (validación y persistencia del
trabajo). Ahí `validated` significa "el lote cumple el contrato de entrada";
aquí se añade el único camino explícito para convertirlo en Customers y
Charges, y el ciclo de vida de los datos temporales que lo alimentan.

## 1. Retención de los datos temporales (TTL)

Los `IMPORT_ROWS` guardan nombre, correo y monto de clientes reales. Antes de
esta rama se retenían indefinidamente.

| Configuración | Valor | Dónde |
| --- | --- | --- |
| `IMPORT_ROWS_RETENTION_DAYS` | **30** (rango 1–365) | Parámetro `ImportRowsRetentionDays` de `template.yaml` → variable de entorno de `StaffApiFunction` |
| Atributo TTL de DynamoDB | `ttl` (número, epoch en segundos) | `TimeToLiveSpecification` de `PaymentsTable` |

**30 días es un valor sandbox de piloto, no una decisión legal ni de
cumplimiento.** Debe revisarse (con asesoría, aviso de privacidad y contrato
con cada negocio) antes de cargar datos reales; cambiarlo es cambiar un
parámetro, no código.

Qué lleva TTL y qué no — a propósito:

| Ítem (todos bajo `PK = BUSINESS#{id}`) | SK | ¿TTL? | ¿PII? |
| --- | --- | --- | --- |
| Chunks de filas | `IMPORT_ROWS#{importId}#{nnnn}` | **Sí** (`ttl = validación + N días`) | **Sí** (nombre, correo, externalId, monto, concepto) |
| Metadatos del trabajo | `IMPORT#{createdAt}#{importId}` | No | No (conteos, nombre de archivo, `sub` opaco, `rows_expire_at`) |
| Guarda de idempotencia (validate) | `IMPORT_KEY#{guardKey}` | No | No |
| Lookup por id | `IMPORT_LOOKUP#{importId}` | No | No |
| Guarda de idempotencia (apply) | `IMPORT_APPLY_KEY#{importId}#{guardKey}` | No | No |
| Resultado por fila del apply | `IMPORT_APPLYROW#{importId}#{chunk}#{pos}` | No | No (ids UUID, código de resultado, `sourceRow`) |
| Auditoría | `AUDIT#import.*` | No | No (conteos; nunca filas) |

DynamoDB borra por TTL de forma asíncrona (puede tardar hasta ~48 h después de
`ttl`). Por eso el servidor **no confía en que el ítem ya no exista**: un chunk
con `ttl <= ahora` se trata como no disponible aunque siga presente, y un
apply sobre filas expiradas se rechaza (`import_rows_unavailable`).

## 2. Purga controlada de un ImportJob

`POST /platform/businesses/{businessId}/imports/{importId}/purge`
(superadmin, `Idempotency-Key` obligatoria, sin cuerpo).

### Qué se borra, qué se conserva

| Se borra | Se conserva |
| --- | --- |
| Chunks `IMPORT_ROWS#{importId}#*` (la PII) | `AUDIT#import.validated` / `import.invalid` (ya existentes) |
| Resultados por fila `IMPORT_APPLYROW#{importId}#*` | `AUDIT#import.apply_started`, `import.applied`/`import.apply_review` |
| Guardas de apply `IMPORT_APPLY_KEY#{importId}#*` | **`AUDIT#import.purged#{importId}`** (evidencia mínima de la purga, creada por ella) |
| Guarda de validate `IMPORT_KEY#…` | Customers y Charges ya creados por el apply (son datos de negocio legítimos; llevan `import_id`/`external_id`, sin copia de la fila) |
| Lookup `IMPORT_LOOKUP#{importId}` y metadatos `IMPORT#…` | |

La evidencia de purga (`details`) contiene: estado previo, cuántos chunks,
resultados y guardas se eliminaron, y el `sub` del superadmin. **Ninguna fila,
nombre, correo ni externalId.** Ese mismo ítem es el "tombstone": un
`GET`/`purge` posterior de un `importId` sin metadatos lo encuentra y responde
`status: "purged"`, así que la purga es idempotente y distingue "purgado" de
"nunca existió" (404). Al borrarse la guarda de validate, repetir el *mismo*
`validate` con la misma clave tras una purga crearía un trabajo nuevo; es
aceptable porque `validated` nunca crea dinero y el apply es explícito.

### Diseño de claves y transacciones

Todo vive en la partición del negocio: **la purga localiza sus ítems con
`Query` sobre `PK` + `begins_with(SK, prefijo)`, nunca con `Scan`**, y otro
negocio es inalcanzable por construcción (`PK`); un `importId` de otro negocio
responde 404 idéntico a uno inexistente.

Máquina de la purga (reanudable, sin referencias huérfanas):

1. `Update` condicional de los metadatos: `status → purging`
   (`purge_prior_status`, actor). Condición: estado ∈ {validated, invalid,
   applied, review, applying} **y** (no `applying` **o** su `lease` expiró).
   **Una purga nunca corre sobre un apply activo.** Un job que aparece en
   `purging` bloquea también cualquier apply nuevo.
2. Borrado de hijos en transacciones de **≤ 90 `Delete`** (< 100 operaciones;
   las claves pesan bytes, muy por debajo de 4 MB). Un job de 500 filas tiene
   ≤ 20 chunks + ≤ 500 resultados ⇒ ~6 transacciones. Los `Delete` sin
   condición son idempotentes: reintentar tras un corte a la mitad no falla.
3. Transacción final atómica: `Put` de la auditoría `import.purged`
   (`attribute_not_exists`) + `Delete` de metadatos (condicionado a
   `status = purging`) + `Delete` de lookup + `Delete` de la guarda de validate.

Los hijos se borran **antes** que el padre y el padre desaparece de forma
atómica con su lookup: nunca queda un chunk o resultado apuntando a metadatos
inexistentes. Si el proceso muere entre pasos, el trabajo queda `purging` y
cualquier llamada de purga lo continúa.

## 3. Apply ETL controlado

`POST /platform/businesses/{businessId}/imports/{importId}/apply`
(solo superadmin, `Idempotency-Key` obligatoria, sin cuerpo).

**Qué crea:** Customers y Charges canónicos **abiertos** (`allocated_minor = 0`,
`outstanding_minor = amount_minor`, sin `cancelled_at`), con folio del contador
del negocio. **Qué nunca crea:** pagos, asignaciones, ajustes, reembolsos,
eventos de proveedor, intentos de pago ni historial financiero. Tampoco crea
**enlaces de pago**: `create_charge` emite un token que solo se muestra una
vez al creador, y un apply no tiene a quién mostrárselo (ni acceso al secreto
HMAC); emitir enlaces para cobros importados es un paso posterior (§7).

### Respuestas al diseño

**1. Cómo se reconoce un Customer ya creado desde el mismo externalId.**
El `customerId` de un cliente importado es **determinista**:
`uuid5(NAMESPACE, json([businessId, "customer", externalId]))` (JSON, no una
cadena con separador: un externalId que contenga el separador no puede
fabricar otra identidad; hay una prueba para esto), con
`SK = CUSTOMER#{customerId}` y atributos `external_id`, `origin=import`,
`import_id`. No hay que buscarlo: se calcula la clave y se hace `GetItem`
consistente. Un cliente creado a mano (sin `external_id`) **no** se
reconoce como el mismo — decisión deliberada: adivinar por nombre es
exactamente la coincidencia silenciosa que se prohíbe.

**2. Cómo se evitan dos Customers si dos trabajos usan el mismo externalId.**
El `Put` del Customer es `attribute_not_exists(PK)` sobre esa clave
determinista: solo una transacción puede crearlo, sea del mismo trabajo,
de otro, o concurrente. La perdedora relee el ítem y lo trata como
"existente" (respuesta 9). No existe un segundo camino que genere un UUID aleatorio
para el mismo externalId.

**3. Cómo se evita crear dos Charges si un apply se reintenta.**
Igual: `chargeId = uuid5(NAMESPACE, json([businessId, "charge", chargeExternalId]))`
y `Put` con `attribute_not_exists`. Además cada fila lleva un **resultado
persistente** (`IMPORT_APPLYROW#…`) escrito **en la misma transacción** que sus
Customer/Charge; el resultado tiene `attribute_not_exists`, así que una fila
ya aplicada nunca se rehace, y el conteo de progreso sale de esos ítems (una
fuente única, no contadores que puedan divergir).

**4. Cómo se procesan hasta 500 filas sin una transacción gigante.**
**Una transacción por fila**, de ≤ 5 ítems (cerco de lease, resultado,
Customer opcional, contador de folio + Charge opcionales). Como 500
transacciones no caben en el `Timeout` de 10 s de la Lambda, el apply se
ejecuta en **rebanadas acotadas** por invocación (`APPLY_ROWS_PER_INVOCATION`
= 50 filas y un presupuesto de tiempo de 5 s, lo que ocurra primero): cada
`POST` procesa una rebanada y devuelve el progreso; el cliente repite el
`POST` con **la misma** `Idempotency-Key` hasta ver `applied`/`review`. Las
filas se leen de chunks de 25 (≤ 20 ítems), nunca un ítem con todo el lote.

**5. Cómo se reanuda un apply que falla a mitad de camino.**
Cada fila ya aplicada tiene su resultado persistente; una rebanada nueva lee
esos resultados, salta lo hecho y continúa con lo pendiente. Un fallo
transitorio (DynamoDB, conflicto agotado) detiene la rebanada, registra
`apply_last_error` y libera el lease; el trabajo queda `applying` y se
reanuda con el mismo `POST`. Nada se re-crea porque las claves son
deterministas.

**6. Estados de ImportJob.**

```
validated ──apply──▶ applying ──(todas las filas decididas, 0 conflictos)──▶ applied
                        │                    └──(≥ 1 conflicto)──────────▶ review
                        └── fallo/corte/lease vencido: sigue en applying (reanudable)
validated | invalid | applied | review | applying(sin lease activo) ──purge──▶ purging ──▶ purged
invalid ─── (no admite apply)
```
`IMPORT_STATES` pasa a `validated, invalid, applying, applied, review,
purging, purged` (`purged` solo existe como evidencia, ya que los metadatos se
borran). `applied` **solo** significa que cada fila terminó `created` o
`reused`. `review` significa que el lote se procesó completo pero ≥ 1 fila
quedó en conflicto; las filas no conflictivas **sí** se crearon.

**7. Cómo se impide apply sobre un lote inválido, purgado o ya aplicado.**
El servidor lee el estado autoritativo (`ConsistentRead`) y solo acepta
`validated` (o `applying` para reanudar). `invalid`, `purging`, `applied`,
`review` → `409 import_not_applicable` sin escribir nada; `purged` (tombstone) →
mismo `409`. Repetir el mismo apply ya terminado con la **misma** clave
devuelve el resultado guardado (idempotente); con otra clave, `409`.
Además el apply **revalida** los datos recuperados antes de escribir (§ abajo)
y el `Update` de arranque es condicional al estado.

**8. Aislamiento por businessId.** Todo ítem, guarda y resultado va bajo
`PK = BUSINESS#{businessId}` tomado de la ruta autenticada; los ids
deterministas incluyen el `businessId`; un `importId` de otro negocio no
existe en esa partición (404). Dos negocios con el mismo externalId producen
ids distintos y no se ven.

**9. Customer existente con identidad conflictiva.** Compatible = mismo
`displayName` normalizado y correos no contradictorios (mismo criterio que la
validación del lote: solo hay conflicto si **ambos** traen correo y difieren;
un correo faltante no contradice). Compatible ⇒ se **reutiliza sin
modificarlo** (nunca se "enriquece" ni sobrescribe un cliente existente).
Incompatible ⇒ la fila termina `conflict` (`customer_identity_conflict`), no se
crea ni el cliente ni su cobro, y el trabajo termina en `review`. Nunca se
elige silenciosamente un registro.

**10. Charge con el mismo externalId y payload distinto.** Se compara
`(customer_id, amount_minor, currency, description, due_date)`. Igual ⇒
`reused` (idempotente, también entre trabajos distintos). Distinto ⇒
`conflict` (`charge_payload_conflict`); un cobro existente pero cancelado ⇒
`conflict` (`charge_cancelled`) — un import no reabre cobros cancelados. Nunca
se sobrescribe.

**11. Auditoría al finalizar.** `AUDIT#import.apply_started#{importId}`
(actor + `Idempotency-Key`) y `AUDIT#import.applied#{importId}` o
`…apply_review#{importId}` con solo conteos (filas, clientes creados/reutilizados,
cobros creados/reutilizados, conflictos). El detalle por fila vive en los
`IMPORT_APPLYROW` (ids y códigos, sin PII), inspeccionables hasta que se
purgue el trabajo; los Customers/Charges conservan `import_id`/`external_id`.

**12. Cómo se evita que el frontend declare éxito antes de evidencia.**
La UI solo muestra "aplicado" cuando la **respuesta del servidor** trae
`status: "applied"` y un `apply` cuyos conteos cuadran con las filas; no hay
estado optimista, ni éxito por "no hubo error", ni por el timeout de una
solicitud incierta (que se muestra como incierto y se reintenta con la misma
clave). `validated` nunca se presenta como `applied`.

### Revalidación en servidor

Antes de tocar nada el apply recarga los chunks (`Query`, sin `Scan`),
descarta cualquier chunk con `ttl` vencido, exige que estén todos
(`ceil(inputRows / 25)`), convierte `Decimal` → `int` (DynamoDB devuelve
`Decimal`; el dinero nunca pasa por `float`) y vuelve a correr
`imports.normalize_record` sobre cada fila con conjuntos "vistos" de todo el
lote, comprobando además que el total de filas y el monto coincidan con el
resumen guardado. Si algo no cuadra, `409` y **no** se entra a `applying`.

### Concurrencia: lease y cerco

Un apply (una rebanada) toma un **lease** (`apply_lease_token`,
`apply_lease_expires_at`, 30 s) con un `Update` condicional. Otro `POST`
concurrente ve el lease vigente y devuelve el progreso sin procesar. Cada
transacción de fila incluye un `Update` de los metadatos condicionado a
`apply_lease_token = :mio AND status = applying` (**cerco**): una rebanada
cuyo lease venció, o un job que pasó a `purging`, no puede seguir escribiendo.
Aun sin el cerco, las claves deterministas impedirían duplicados; el cerco
protege la purga y mantiene el progreso limpio.

## 4. Límites de DynamoDB

| Operación | Ítems por transacción | Bytes |
| --- | --- | --- |
| Crear el trabajo (existente) | ≤ 4 + 20 chunks = 24 | ~700 KB peor caso |
| Fila del apply | ≤ 5 | < 2 KB |
| Arranque del apply | ≤ 3 | < 2 KB |
| Cierre del apply | 2 | < 2 KB |
| Purga (lote de borrado) | ≤ 90 | claves solamente |

Todos < 100 ítems y < 4 MB. Ninguna operación usa `Scan`.

## 5. IAM y plantilla

`StaffApiFunction` ya tenía `GetItem/PutItem/Query/UpdateItem/TransactWriteItems`;
la purga añade **`dynamodb:DeleteItem`** (necesario para los `Delete` dentro de
`TransactWriteItems`). Se añaden las rutas `POST …/apply` y `POST …/purge`
(mismo autorizador JWT + grupo de plataforma en servidor), el parámetro de
retención y `TimeToLiveSpecification`. Ningún despliegue se ejecutó.

## 6. Evidencia de pruebas

| Área | Archivo | Qué prueba |
| --- | --- | --- |
| Reglas puras del apply | `backend/tests/unit/test_import_apply_rules.py` | Ids deterministas (incl. separador forjado), compatibilidad de identidad, decisión por fila, revalidación de filas guardadas (manipuladas, duplicadas, flotantes) |
| TTL y purga | `backend/tests/integration/test_import_retention.py` | `ttl` numérico solo en chunks; sin PII fuera de los chunks; retención configurable y validada; chunk vencido pero no borrado nunca se sirve; purga completa, idempotente, sin huérfanos, sin `Scan`, ≤ 100 ops/4 MB con 500 filas, reanudable tras un corte, rechazada con lease vivo, aislada entre negocios, autorizada solo a superadmin |
| Apply | `backend/tests/integration/test_import_apply.py` | Crea lo esperado y nada financiero; misma clave no duplica; reanudación; concurrencia (lease, cerco, cliente/folio en carrera); conflictos de identidad y de payload; rechazo de inválido/purgado/aplicado/expirado/manipulado; fallo parcial recuperable; 500 filas en 10 rebanadas con transacciones ≤ 6 ítems; HTTP y aislamiento por negocio |
| Transporte HTTP frontend | `staff-workspace/src/api/http.test.ts` | Timeout, cancelación, error de red, 4xx/5xx, reintento con la misma `Idempotency-Key`, ambos clientes |
| Flujo ETL frontend | `staff-workspace/src/pages/implementation-utils.test.ts`, `apply-workflow.test.ts` | Cancelación por cambio de negocio/desmontaje sin actualizar estado; éxito solo con evidencia del servidor; bucle de apply (incierto, pausado, cancelado, acotado) |

Se verificó por **mutación** que las pruebas de concurrencia no pasan en vacío:
quitar la condición del cerco del apply hace fallar
`test_a_displaced_apply_cannot_add_new_rows_to_a_job_a_purge_has_already_fenced`
(una primera versión de esas pruebas sobrevivía a la mutación porque la
condición del resultado ya impedía duplicados; se añadió la que prueba el valor
real del cerco: no dejar huérfanos tras la purga).

## 7. Deuda restante y por qué **no** hay luz verde para datos reales

Nada de esto es opcional antes de datos reales:

1. **Ninguna prueba contra AWS/DynamoDB real.** El vencimiento efectivo por TTL
   (y su retraso de hasta ~48 h), las transacciones y la concurrencia solo se
   probaron con Moto, que ejecuta las transacciones de forma secuencial. Las
   carreras se simulan con intercalados deterministas, no con hilos reales; no
   existe un equivalente de `test_real_dynamodb_refund_concurrency.py` para
   apply/purge. Moto además copia la tabla en cada transacción: la prueba de
   500 filas tarda ~140 s allí y no mide el rendimiento real; el presupuesto de
   50 filas / 5 s por invocación es una estimación contra el `Timeout` de 10 s.
2. **Sin prueba de navegador contra un sandbox desplegado.** La UI se validó
   con pruebas unitarias de su lógica y un recorrido manual en modo fixtures
   (validar → confirmar → aplicar → «Aplicado»); no hay pruebas de
   componentes (sin jsdom/testing-library) ni E2E automatizadas.
3. **Los 30 días son un valor de sandbox, no una decisión legal.** Además, los
   Customers/Charges creados por un apply son datos de negocio permanentes con
   PII (nombre, correo) y no tienen política de retención; cambiar
   `ImportRowsRetentionDays` no afecta a chunks ya escritos (el `ttl` se fija
   al crearlos).
4. **Enlaces de pago:** un cobro importado no tiene enlace (ver §3); falta el
   paso que los emita.
5. **Conflictos:** `review` es terminal; no hay resolución guiada (elegir el
   registro existente / corregir y reintentar una fila). Un cliente creado a
   mano nunca se reconoce por nombre; solo por `external_id`.
6. **Purga de un apply a medias** descarta las filas pendientes (lo ya creado
   permanece); la evidencia lo registra (`priorStatus = applying`).
7. **Límites de lectura:** la lista de trabajos no trae progreso (solo el
   detalle); la muestra de conflictos se limita a 50; la UI abre solo los 20
   trabajos más recientes de la lista.
8. **Sin S3/`.xlsx`** ni subida temporal segura (ya listado en el documento
   del 2026-09-14, §11).
9. **Operación:** sin alarma específica para trabajos que quedan en `applying`
   o `purging` (no hay un barrido que los reanude); dependen de que una
   persona repita la llamada.

Sigue sin desplegarse nada, sin usar AWS real y sin publicar la rama.
