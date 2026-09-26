import { useEffect, useRef, useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { usePlatformApi } from "../app-context";
import { isRequestCancelled } from "../api/http";
import type { ImportJobView } from "../api/platform-contracts";
import { getOrCreateOperation, newKey } from "../lib/idempotency";
import { formatMxn } from "../lib/money";
import {
  applyErrorMessage, conflictReasonLabel, describeApply, purgeErrorMessage, runApplyLoop, type ApplyFailureOutcome,
} from "./apply-workflow";
import { createRequestScope, platformImportsQueryKey, trackMount } from "./implementation-utils";

type Failure = ApplyFailureOutcome | { kind: "paused"; errorCode: string | null } | { kind: "stalled" };

function failureMessage(failure: Failure): string {
  if (failure.kind === "paused") {
    return `El servidor detuvo la aplicación por un error recuperable${failure.errorCode ? ` (${failure.errorCode})` : ""}. Lo ya creado se conserva. Pulsa Reintentar: se usa la misma clave y se continúa donde se quedó, sin duplicar.`;
  }
  if (failure.kind === "stalled") return "El servidor respondió varias veces sin avanzar. Espera un momento y pulsa Reintentar; no se duplicará nada.";
  return applyErrorMessage(failure);
}

/** Everything about applying (or purging) ONE server import job.
 *
 * What it shows about the job comes only from server responses: `current`
 * starts as the job the server gave us and changes only when the server
 * answers again (apply progress, purge). No click, timeout or error here ever
 * sets an "applied" state. It is keyed by business+import by its parent, so a
 * change of business/job unmounts it -- which cancels its in-flight request
 * and stops its loop before it can touch state. */
export function ApplyPanel({ businessId, businessName, initialJob, preview, onChange }: {
  businessId: string;
  businessName: string;
  initialJob: ImportJobView;
  /** Local, unverified counts from the rows on screen (absent when a job is
   * opened from the server list, where only the server summary exists). */
  preview?: { rows: number; customers: number; totalMinor: number };
  onChange?: (job: ImportJobView) => void;
}) {
  const platformApi = usePlatformApi();
  const queryClient = useQueryClient();
  const [current, setCurrent] = useState<ImportJobView>(initialJob);
  const [running, setRunning] = useState(false);
  const [purging, setPurging] = useState(false);
  const [failure, setFailure] = useState<Failure | null>(null);
  const [purgeError, setPurgeError] = useState<string | null>(null);
  const [confirmed, setConfirmed] = useState(false);
  const [purgeConfirmed, setPurgeConfirmed] = useState(false);
  const isMountedRef = useRef(true);
  useEffect(() => trackMount(isMountedRef), []);
  const scopeRef = useRef(createRequestScope());
  useEffect(() => () => scopeRef.current.cancel(), []);

  const importId = initialJob.importId;
  const phase = describeApply(current);
  const busy = running || purging;

  function refreshServerList() {
    void queryClient.invalidateQueries({ queryKey: platformImportsQueryKey(businessId, platformApi.source) });
  }

  async function apply() {
    if (busy) return;
    setRunning(true);
    setFailure(null);
    const signal = scopeRef.current.begin();
    // One key per (business, import), kept until the tab closes: a retry after
    // a timeout, an error or a reload sends the same Idempotency-Key.
    const operation = getOrCreateOperation("apply-import", `${businessId}:${importId}`, () => ({ key: newKey("import-apply") }));
    try {
      const outcome = await runApplyLoop({
        applyImport: (options) => platformApi.applyImport(businessId, importId, operation.key, options),
        onProgress: (next) => {
          if (!isMountedRef.current) return;
          setCurrent(next);
          onChange?.(next);
        },
        isActive: () => isMountedRef.current && !signal.aborted,
        signal,
      });
      if (!isMountedRef.current) return;
      if (outcome.kind === "uncertain" || outcome.kind === "refused") setFailure(outcome);
      else if (outcome.kind === "paused") setFailure({ kind: "paused", errorCode: outcome.job.status !== "purged" ? outcome.job.apply?.lastErrorCode ?? null : null });
      else if (outcome.kind === "stalled" || outcome.kind === "exhausted") setFailure({ kind: "stalled" });
    } finally {
      // The server may have advanced even if the request was cut short.
      refreshServerList();
      if (isMountedRef.current) setRunning(false);
    }
  }

  async function purge() {
    if (busy) return;
    setPurging(true);
    setPurgeError(null);
    const signal = scopeRef.current.begin();
    const operation = getOrCreateOperation("purge-import", `${businessId}:${importId}`, () => ({ key: newKey("import-purge") }));
    try {
      const purged = await platformApi.purgeImport(businessId, importId, operation.key, { signal });
      if (isMountedRef.current) { setCurrent(purged); onChange?.(purged); }
    } catch (error) {
      if (!isRequestCancelled(error) && isMountedRef.current) setPurgeError(purgeErrorMessage(error));
    } finally {
      refreshServerList();
      if (isMountedRef.current) setPurging(false);
    }
  }

  if (current.status === "purged") {
    return <section className="platform-card apply-panel" aria-label="Aplicación del lote">
      <p className="eyebrow">DATOS TEMPORALES</p>
      <h2>Datos eliminados</h2>
      <p className="field-help">El servidor eliminó las filas temporales de este trabajo el {new Date(current.purgedAt).toLocaleString("es-MX")}. Solo queda evidencia mínima de auditoría, sin datos personales. Este trabajo ya no se puede aplicar. Los clientes y cobros que se hubieran creado antes permanecen en el negocio.</p>
    </section>;
  }

  const job = current;
  const summary = { rows: job.summary.validRows, totalMinor: job.summary.totalMinor };
  const progress = job.apply;
  const canStart = phase.kind === "ready";
  const canResume = phase.kind === "resumable";
  const canPurge = phase.kind !== "in_progress" && phase.kind !== "purging";

  return <section className="platform-card apply-panel" aria-label="Aplicación del lote">
    <p className="eyebrow">APLICAR AL NEGOCIO</p>
    <h2>Crear clientes y cobros</h2>

    {(canStart || canResume) && <div className="apply-preview">
      <h3>Vista previa de lo que se creará en {businessName}</h3>
      <div className="result-metrics">
        <div><small>Cobros abiertos</small><strong>{summary.rows}</strong></div>
        <div><small>Clientes distintos</small><strong>{preview ? preview.customers : "—"}</strong></div>
        <div><small>Monto total</small><strong>{formatMxn(summary.totalMinor)}</strong></div>
        <div><small>Negocio destino</small><strong className="apply-business"><code>{businessId}</code></strong></div>
      </div>
      {!preview && <p className="field-help">Clientes distintos no se muestra: este trabajo se abrió desde el servidor, que solo guarda el resumen. Un cliente que ya exista con la misma clave se reutiliza, no se duplica.</p>}
    </div>}

    {canStart && <div className="apply-warning" role="note">
      <strong>Aplicar sí crea datos reales.</strong> Se crearán clientes y cobros <b>abiertos</b> con saldo pendiente en este negocio. No se crean pagos, asignaciones, reembolsos ni enlaces de pago, y esta pantalla no los deshace. Un cliente o cobro que ya exista con la misma clave y los mismos datos se reutiliza; si difiere, esa fila queda en conflicto y no se modifica.
    </div>}
    {canStart && <label className="apply-confirm"><input type="checkbox" checked={confirmed} onChange={(event) => setConfirmed(event.target.checked)} disabled={busy} /> Entiendo que se crearán {summary.rows} cobros abiertos por {formatMxn(summary.totalMinor)} en <b>{businessName}</b>.</label>}
    {canStart && <button className="button primary" disabled={!confirmed || busy} onClick={() => void apply()}>{running ? "Aplicando en el servidor…" : "Aplicar importación"}</button>}

    {canResume && <p className="field-help">La aplicación de este trabajo quedó a medias en el servidor. Lo ya creado se conserva; continuar usa la misma clave y no duplica nada.</p>}
    {canResume && <button className="button primary" disabled={busy} onClick={() => void apply()}>{running ? "Aplicando en el servidor…" : failure || progress?.failed ? "Reintentar" : "Continuar aplicación"}</button>}

    {phase.kind === "in_progress" && <p className="field-help" role="status">Otra aplicación de este lote está en curso en el servidor. Espera a que termine o vuelve a abrir el trabajo.</p>}

    {progress && (phase.kind === "in_progress" || phase.kind === "resumable" || running) && <div className="apply-progress" role="status" aria-live="polite">
      <progress max={progress.rowsTotal} value={progress.rowsProcessed} />
      <p><strong>{progress.rowsProcessed} de {progress.rowsTotal}</strong> filas decididas por el servidor</p>
    </div>}
    {running && !progress && <p className="field-help" role="status">Enviando la orden de aplicar al servidor…</p>}

    {failure && <p className="inline-error" role="alert">{failureMessage(failure)}</p>}

    {phase.kind === "applied" && <div className="apply-banner success" role="status">
      <strong>El servidor confirmó la aplicación.</strong> Se decidieron las {phase.progress.rowsTotal} filas: {phase.progress.chargesCreated} cobros creados y {phase.progress.chargesReused} ya existentes reutilizados; {phase.progress.customersCreated} clientes creados y {phase.progress.customersReused} reutilizados. Sin conflictos.
    </div>}
    {phase.kind === "review" && <div className="apply-banner warning" role="status">
      <strong>Aplicado con conflictos.</strong> El servidor decidió las {phase.progress.rowsTotal} filas: creó {phase.progress.chargesCreated} cobros (reutilizó {phase.progress.chargesReused}) y dejó <b>{phase.progress.conflicts}</b> fila(s) sin crear ni modificar. Las filas sin conflicto sí se crearon. Un conflicto nunca se resuelve solo: revisa el registro existente y, si corresponde, prepara un lote nuevo.
    </div>}
    {phase.kind === "unverified" && <div className="apply-banner danger" role="alert">
      <strong>No se puede confirmar este resultado.</strong> El servidor reporta el trabajo como {job.status === "review" ? "con conflictos" : "aplicado"}, pero sus conteos no coinciden con lo validado. No lo des por bueno: revisa el negocio antes de continuar.
    </div>}
    {phase.kind === "invalid" && <p className="inline-error" role="alert">Este lote es inválido y no se puede aplicar. Corrige el archivo y valida de nuevo.</p>}
    {phase.kind === "purging" && <p className="field-help" role="status">El servidor está eliminando los datos temporales de este trabajo; no se puede aplicar.</p>}

    {progress && progress.conflictSample.length > 0 && <div className="issue-table conflict-list">
      <div className="issue-head"><span>Fila</span><span>Motivo</span></div>
      {progress.conflictSample.map((conflict) => <div key={`${conflict.sourceRow}-${conflict.code}`}><strong>{conflict.sourceRow}</strong><p>{conflictReasonLabel(conflict.code)}</p></div>)}
      {progress.conflictsTruncated && <p className="field-help">Se muestran solo los primeros conflictos; el conteo de arriba es exacto.</p>}
    </div>}

    <div className="apply-retention">
      <p className="field-help">{job.rowsExpireAt
        ? <>Las filas temporales de este lote (con datos de clientes) se eliminan solas el {new Date(job.rowsExpireAt).toLocaleDateString("es-MX")}. Puedes eliminarlas antes.</>
        : <>Las filas temporales de este lote se eliminan automáticamente.</>}</p>
      {canPurge && <>
        <label className="apply-confirm"><input type="checkbox" checked={purgeConfirmed} onChange={(event) => setPurgeConfirmed(event.target.checked)} disabled={busy} /> Quiero eliminar ahora los datos temporales de este lote.</label>
        <button className="button secondary" disabled={!purgeConfirmed || busy} onClick={() => void purge()}>{purging ? "Eliminando…" : "Eliminar datos temporales"}</button>
      </>}
      {purgeError && <p className="inline-error" role="alert">{purgeError}</p>}
    </div>
  </section>;
}
