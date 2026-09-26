import { ApiError, isRequestCancelled } from "../api/http";
import type { ApplyProgress, ImportJob, ImportJobStatus, ImportJobView, PurgedImport } from "../api/platform-contracts";

// The whole point of this module: the UI may show an import as applied only
// when the SERVER says so, with evidence that adds up. Nothing here (or in the
// page) sets an "applied" state on its own -- not after a click, not after a
// request that timed out. A response is the only input.

export type ApplyPhase =
  | { kind: "ready" }
  | { kind: "in_progress"; progress: ApplyProgress | null }
  | { kind: "resumable"; progress: ApplyProgress; failed: boolean; errorCode: string | null }
  | { kind: "applied"; progress: ApplyProgress }
  | { kind: "review"; progress: ApplyProgress }
  /** The server said applied/review but the evidence is missing or does not
   * add up. Shown as "verify", never as success. */
  | { kind: "unverified" }
  | { kind: "invalid" }
  | { kind: "purging" }
  | { kind: "purged" };

function isPurged(job: ImportJobView): job is PurgedImport {
  return job.status === "purged";
}

/** Whether the server-reported progress proves a complete, clean apply:
 * every validated row was decided, every one created or reused a Charge,
 * nothing conflicted or failed, and the totals match the job's own summary. */
function completeAndClean(job: ImportJob, progress: ApplyProgress): boolean {
  return progress.rowsTotal === job.summary.validRows
    && progress.rowsProcessed === progress.rowsTotal
    && progress.conflicts === 0
    && progress.failed === 0
    && progress.chargesCreated + progress.chargesReused === progress.rowsTotal;
}

export function describeApply(job: ImportJobView): ApplyPhase {
  if (isPurged(job)) return { kind: "purged" };
  switch (job.status) {
    case "validated": return { kind: "ready" };
    case "invalid": return { kind: "invalid" };
    case "purging": return { kind: "purging" };
    case "applying": {
      if (!job.apply) return { kind: "in_progress", progress: null };
      if (job.apply.leaseActive) return { kind: "in_progress", progress: job.apply };
      return { kind: "resumable", progress: job.apply, failed: job.apply.failed > 0, errorCode: job.apply.lastErrorCode };
    }
    case "applied":
      return job.apply && completeAndClean(job, job.apply) ? { kind: "applied", progress: job.apply } : { kind: "unverified" };
    case "review":
      return job.apply && job.apply.conflicts > 0 && job.apply.failed === 0 && job.apply.rowsProcessed === job.apply.rowsTotal
        ? { kind: "review", progress: job.apply }
        : { kind: "unverified" };
  }
}

/** True only for a server-confirmed, complete, conflict-free apply. */
export function isApplySuccess(job: ImportJobView): boolean {
  return describeApply(job).kind === "applied";
}

export type ApplyLoopDeps = {
  /** One bounded server slice. The caller binds the SAME Idempotency-Key to
   * every call (lib/idempotency.ts), so a repeat after an uncertain outcome
   * can never duplicate anything. */
  applyImport: (options: { signal?: AbortSignal }) => Promise<ImportJobView>;
  /** Every server response, in order. The only channel for progress. */
  onProgress: (job: ImportJobView) => void;
  /** False once the screen no longer shows this import (business changed,
   * unmounted): the loop stops and touches nothing. */
  isActive: () => boolean;
  signal?: AbortSignal;
  wait?: (ms: number, signal?: AbortSignal) => Promise<void>;
  /** Delay before polling while another invocation holds the lease. */
  pollMs?: number;
  maxCalls?: number;
};

export type ApplyLoopOutcome =
  | { kind: "finished"; job: ImportJobView }
  /** The server reported a recoverable failure; the user decides to retry. */
  | { kind: "paused"; job: ImportJobView }
  /** No HTTP answer (timeout/network): the slice may or may not have run. */
  | { kind: "uncertain"; error: ApiError }
  /** The server answered with an error (409, 403, 5xx...). */
  | { kind: "refused"; error: ApiError }
  /** Intentional: context changed or unmounted. Silent. */
  | { kind: "cancelled" }
  /** The server keeps answering without advancing. */
  | { kind: "stalled"; job: ImportJobView }
  | { kind: "exhausted"; job: ImportJobView };

export const DEFAULT_APPLY_POLL_MS = 1500;
/** 500 rows / 50 per slice = 10, and a 5 s server budget can shrink a slice;
 * this only guards against a runaway loop, it is not a pacing knob. */
export const DEFAULT_MAX_APPLY_CALLS = 200;
const STALL_LIMIT = 3;

function defaultWait(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal?.aborted) return resolve();
    const timer = setTimeout(() => { signal?.removeEventListener("abort", onAbort); resolve(); }, ms);
    const onAbort = () => { clearTimeout(timer); resolve(); };
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}

function processedOf(job: ImportJobView): number {
  return isPurged(job) ? -1 : job.apply?.rowsProcessed ?? 0;
}

export async function runApplyLoop(deps: ApplyLoopDeps): Promise<ApplyLoopOutcome> {
  const { signal, isActive } = deps;
  const wait = deps.wait ?? defaultWait;
  const pollMs = deps.pollMs ?? DEFAULT_APPLY_POLL_MS;
  const maxCalls = deps.maxCalls ?? DEFAULT_MAX_APPLY_CALLS;
  let last: ImportJobView | null = null;
  let stalledFor = 0;

  for (let call = 0; call < maxCalls; call += 1) {
    if (!isActive() || signal?.aborted) return { kind: "cancelled" };
    let job: ImportJobView;
    try {
      job = await deps.applyImport({ signal });
    } catch (error) {
      if (isRequestCancelled(error) || !isActive()) return { kind: "cancelled" };
      if (error instanceof ApiError) return error.status === 0 ? { kind: "uncertain", error } : { kind: "refused", error };
      throw error;
    }
    // A response that lands after the screen moved on is discarded whole.
    if (!isActive() || signal?.aborted) return { kind: "cancelled" };
    deps.onProgress(job);

    const phase = describeApply(job);
    if (phase.kind !== "in_progress" && phase.kind !== "resumable") return { kind: "finished", job };
    if (phase.kind === "resumable" && phase.failed) return { kind: "paused", job };

    stalledFor = last !== null && processedOf(job) === processedOf(last) ? stalledFor + 1 : 0;
    last = job;
    if (stalledFor >= STALL_LIMIT) return { kind: "stalled", job };
    if (phase.kind === "in_progress") await wait(pollMs, signal);
  }
  return { kind: "exhausted", job: last as ImportJobView };
}

/** Local, unverified preview of what a batch is about to do -- counts from
 * the rows on screen. The server's own summary is what the job reports. */
export function summarizePreview(rows: ReadonlyArray<{ customer: { externalId: string }; charge: { amountMinor: number } }>): { rows: number; customers: number; totalMinor: number } {
  return {
    rows: rows.length,
    customers: new Set(rows.map((row) => row.customer.externalId)).size,
    totalMinor: rows.reduce((sum, row) => sum + row.charge.amountMinor, 0),
  };
}

export type StatusTone = "neutral" | "success" | "warning" | "danger";

export function applyStatusPill(status: ImportJobStatus | "purged"): { tone: StatusTone; label: string } {
  switch (status) {
    case "validated": return { tone: "neutral", label: "Validado · sin aplicar" };
    case "invalid": return { tone: "danger", label: "Inválido" };
    case "applying": return { tone: "neutral", label: "Aplicando…" };
    case "applied": return { tone: "success", label: "Aplicado" };
    case "review": return { tone: "warning", label: "Aplicado con conflictos" };
    case "purging": return { tone: "neutral", label: "Eliminando datos…" };
    case "purged": return { tone: "neutral", label: "Datos eliminados" };
  }
}

export function conflictReasonLabel(code: string): string {
  switch (code) {
    case "customer_identity_conflict": return "Ya existe un cliente con esa clave y otro nombre o correo. No se creó nada para esta fila.";
    case "charge_payload_conflict": return "Ya existe un cobro con esa referencia y datos distintos (monto, concepto o fecha). No se modificó.";
    case "charge_cancelled": return "Ya existe un cobro cancelado con esa referencia; una importación no lo reabre.";
    case "customer_missing": return "El cobro ya existe pero su cliente no; hay que revisarlo a mano.";
    default: return "Esta fila quedó en conflicto. Revisa el registro existente antes de decidir.";
  }
}

export type ApplyFailureOutcome = Extract<ApplyLoopOutcome, { kind: "uncertain" | "refused" }>;

export function applyErrorMessage(outcome: ApplyFailureOutcome): string {
  if (outcome.kind === "uncertain") {
    const cause = outcome.error.code === "request_timeout" ? "El servidor tardó demasiado en responder." : "No hubo conexión con el servidor.";
    return `${cause} La aplicación pudo haber avanzado: no la damos por hecha ni por fallida. Pulsa Reintentar; se usa la misma clave, así que no se duplicará ningún cliente ni cobro.`;
  }
  const { status, code } = outcome.error;
  if (status === 401) return "Tu sesión expiró. Vuelve a iniciar sesión y reintenta; se conserva la misma clave.";
  if (status === 403) return "Tu cuenta no tiene permiso de plataforma para aplicar importaciones.";
  if (code === "import_not_applicable") return "Este lote ya no se puede aplicar: no está validado, ya se aplicó o se eliminaron sus datos.";
  if (code === "import_rows_unavailable") return "Los datos temporales de este lote expiraron o ya no están completos. Vuelve a validar el archivo.";
  if (code === "import_data_invalid") return "Los datos guardados de este lote ya no coinciden con lo validado. No se creó nada; vuelve a validar el archivo.";
  if (code === "import_apply_active") return "Hay una aplicación en curso para este lote. Espera a que termine.";
  if (status === 404) return "El servidor no encontró este trabajo para el negocio seleccionado.";
  return "El servidor no pudo aplicar el lote. Revisa el estado del trabajo antes de reintentar.";
}

export function purgeErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 0) return "No recibimos respuesta del servidor. Puede que los datos ya se hayan eliminado: pulsa de nuevo y se usa la misma clave; eliminar es seguro de repetir.";
    if (error.status === 401) return "Tu sesión expiró. Vuelve a iniciar sesión y reintenta.";
    if (error.status === 403) return "Tu cuenta no tiene permiso de plataforma para eliminar datos temporales.";
    if (error.code === "import_apply_active") return "Hay una aplicación en curso para este lote; espera a que termine antes de eliminar sus datos.";
    if (error.status === 404) return "El servidor no encontró este trabajo para el negocio seleccionado.";
  }
  return "No se pudieron eliminar los datos temporales. No se confirmó ningún borrado; reintenta.";
}
