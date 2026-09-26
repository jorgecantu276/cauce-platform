import { useEffect, useMemo, useRef, useState } from "react";
import type { ChangeEvent } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { usePlatformApi } from "../app-context";
import { useAuth } from "../auth/auth-context";
import { ApiError } from "../api/http";
import type { ImportJobView } from "../api/platform-contracts";
import { EmptyState, ErrorState, LoadingState } from "../components/states";
import { formatMxn } from "../lib/money";
import { ApplyPanel } from "./apply-panel";
import { applyStatusPill, describeApply, summarizePreview } from "./apply-workflow";
import {
  canonicalFields, parseTabularText, suggestMapping, validateImport,
  type CanonicalField, type HeaderMapping, type ImportProfile, type ImportResult,
} from "../etl/import-engine";
import {
  appendAuditEntry, applyFileRead, businessVerificationBadge, createRequestScope, createSubmissionGuard, defaultBusinessSelection,
  filterAuditEntriesForBusiness, platformImportsQueryKey, resolveBusinessId, sourceFormat, submitValidationBatch,
  trackMount,
  type BusinessMembership, type BusinessSelection, type BusinessVerification, type LocalAuditEntry,
} from "./implementation-utils";

function submitErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) return "Tu sesión expiró. Vuelve a iniciar sesión para continuar.";
    if (error.status === 403) return "Tu cuenta no tiene permiso de plataforma para validar lotes en el servidor.";
    if (error.status === 409) return "Esta clave de idempotencia ya se usó con un lote distinto. Ajusta el archivo o espera a que termine el intento anterior.";
    if (error.code === "request_timeout") return "El servidor tardó demasiado en responder. Puede que el lote sí se haya recibido: vuelve a validar y se reutilizará la misma clave, sin duplicar el trabajo.";
    if (error.status === 0) return "Sin conexión. El lote no se envió al servidor; vuelve a intentarlo.";
  }
  return "No se pudo validar el lote en el servidor. No se guardó nada.";
}

type Section = "imports" | "rules" | "businesses" | "audit";

const labels: Record<CanonicalField, string> = {
  customerExternalId: "Clave externa del cliente",
  customerName: "Nombre del cliente",
  customerEmail: "Correo (opcional)",
  chargeExternalId: "Referencia externa del cobro",
  amount: "Monto",
  description: "Concepto",
  dueDate: "Fecha de vencimiento",
};

const sample = [
  "Clave cliente\tCliente\tCorreo\tFolio\tMonto\tConcepto\tVencimiento",
  "CLI-1001\tFerretería del Norte\tcobros@ferreteria.test\tFAC-2026-018\t12500.00\tMaterial de septiembre\t2026-09-30",
  "CLI-1002\tTransportes Río Bravo\tadministracion@transportes.test\tFAC-2026-019\t8740.50\tServicio de mantenimiento\t2026-10-05",
].join("\n");

const initialProfile: ImportProfile = { name: "Cobranza estándar MX", delimiter: "auto", dateFormat: "iso", decimalSeparator: "dot", currency: "MXN" };

export function ImplementationPage() {
  const { session } = useAuth();
  const platformApi = usePlatformApi();
  const queryClient = useQueryClient();
  const memberships: BusinessMembership[] = useMemo(
    () => session?.memberships.map((membership) => ({ businessId: membership.businessId, businessName: membership.businessName })) ?? [],
    [session],
  );
  const [section, setSection] = useState<Section>("imports");
  const [text, setText] = useState(sample);
  const [profile, setProfile] = useState<ImportProfile>(initialProfile);
  const [mapping, setMapping] = useState<HeaderMapping>({});
  const [result, setResult] = useState<ImportResult | null>(null);
  const [fileName, setFileName] = useState("Datos pegados desde Excel");
  const [fileReadError, setFileReadError] = useState<string | null>(null);
  const [audit, setAudit] = useState<LocalAuditEntry[]>([]);
  const [job, setJob] = useState<ImportJobView | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<unknown>(null);
  const [verification, setVerification] = useState<BusinessVerification>({ status: "unverified" });
  const [businessSelection, setBusinessSelection] = useState<BusinessSelection>(() => defaultBusinessSelection(memberships));
  // Hallazgo 1: two separate monotonic generation counters. `contentGuard`
  // is bumped by any change that should invalidate whatever validate-import
  // request is currently in flight (switching business, loading a new
  // file, editing text/mapping/profile) *and* by starting a new
  // submission; `submissionGuard` is bumped only by starting a new
  // submission, so an unrelated content change can never leave the
  // submitting indicator stuck on -- see submitValidationBatch in
  // implementation-utils.ts for exactly how each is used.
  const contentGuardRef = useRef(createSubmissionGuard());
  const submissionGuardRef = useRef(createSubmissionGuard());
  // Hallazgo 3 (second independent review): reading a selected file's text
  // is its own async operation, judged against other file reads only --
  // see applyFileRead in implementation-utils.ts.
  const fileReadGuardRef = useRef(createSubmissionGuard());
  // P0 (second independent review): main.tsx renders under <StrictMode>,
  // which double-invokes this effect (setup/cleanup/setup) once in
  // development -- trackMount's setup marks mounted=true every time it
  // runs, so the second setup call correctly undoes the first cleanup,
  // instead of leaving isMountedRef stuck at false forever.
  const isMountedRef = useRef(true);
  useEffect(() => trackMount(isMountedRef), []);
  // Alcance 1: the AbortController of the one validate-import request that
  // may be in flight. Any context change (invalidateEditableContext) and
  // unmount cancel it, so a request for a business/file no longer on screen
  // stops instead of holding "Validando…" until the timeout.
  const requestScopeRef = useRef(createRequestScope());
  useEffect(() => () => requestScopeRef.current.cancel(), []);

  // P1 (second independent review): the single place that invalidates
  // whatever is currently on screen -- job, result, and any submit error
  // -- because the editable content it was produced from (business, file,
  // text, mapping, or profile) just changed. Deliberately does not touch
  // `audit` (the auditable local history) or `verification` (per-business
  // server-confirmation state): those are keyed by businessId, not by
  // "what is currently being edited", so a content change alone must never
  // clear them.
  function invalidateEditableContext() {
    contentGuardRef.current.bump();
    fileReadGuardRef.current.bump();
    // Cancelling makes the in-flight submission's own `finally` release the
    // submitting indicator; setting it here too keeps the button usable at
    // once, without waiting for that microtask.
    requestScopeRef.current.cancel();
    setSubmitting(false);
    setResult(null);
    setJob(null);
    setSubmitError(null);
  }

  const parsed = useMemo(() => {
    try { return { table: parseTabularText(text, profile.delimiter), error: null }; }
    catch (error) { return { table: null, error: error instanceof Error ? error.message : "No pudimos leer el archivo." }; }
  }, [profile.delimiter, text]);
  const headerSignature = parsed.table?.headers.join("\u001f") ?? "";
  useEffect(() => {
    if (parsed.table) setMapping(suggestMapping(parsed.table.headers));
    invalidateEditableContext();
    // A new source schema deliberately resets mappings instead of silently
    // retaining columns from the previous company/profile.
  }, [headerSignature]); // eslint-disable-line react-hooks/exhaustive-deps

  const businessId = resolveBusinessId(memberships, businessSelection);
  const activeMembership = memberships.find((membership) => membership.businessId === businessId);
  const businessName = activeMembership?.businessName ?? (businessId ? "Negocio sin membership (acceso de plataforma)" : "Sin negocio seleccionado");

  function changeBusiness(next: BusinessSelection) {
    setBusinessSelection(next);
    // Hallazgo 3 / Hallazgo 1: switching destination business must never
    // carry over another business's in-progress result, error, or pending
    // submission state -- each of those belongs to whichever businessId
    // was active when it was produced, not to whatever is selected now.
    // (audit history and per-business verification are intentionally left
    // alone -- see invalidateEditableContext.)
    invalidateEditableContext();
  }

  async function runDryRun() {
    if (!parsed.table || !businessId) return;
    const next = validateImport({ businessId, table: parsed.table, mapping, profile });
    setResult(next);
    setJob(null);
    setSubmitError(null);
    // Hallazgo 2: the local audit entry always captures the businessId (and
    // name, when known) directly from this closure's `businessId`/
    // `businessName` -- never inferred from `next.rows[0].businessId`,
    // which would not exist for an invalid batch with zero rows.
    // P3: appendAuditEntry caps entries per business, not globally, so
    // this business's validations can never push another business's
    // recent history out of the array.
    setAudit((current) => appendAuditEntry(current, { at: new Date().toISOString(), businessId, businessName, file: fileName, result: next }));
    // A locally-invalid batch is never sent: the point of the local dry run
    // is to let staff fix obvious problems without a round trip. Once it is
    // clean, the server still re-validates everything from scratch -- see
    // platform.py/imports.py -- this is never treated as pre-approved.
    if (next.issues.length > 0 || next.rows.length === 0) return;
    // Hallazgo 1: this submission's own identity on each guard. Any later
    // change to business/file/text/mapping/profile bumps contentGuard past
    // contentRequestId, so a response that lands after that never
    // overwrites job/error for whatever context is on screen by then --
    // but submissionGuard/submissionRequestId is untouched by those same
    // changes, so the submitting indicator still reliably turns off.
    const contentRequestId = contentGuardRef.current.bump();
    const submissionRequestId = submissionGuardRef.current.bump();
    const signal = requestScopeRef.current.begin();
    const submittedBusinessId = businessId;
    const source = { fileName, format: sourceFormat(fileName) };
    const records = next.rows.map(({ businessId: _businessId, ...record }) => record);
    setSubmitting(true);
    await submitValidationBatch(
      { businessId: submittedBusinessId, source, profile, mapping, text, records },
      {
        validateImport: (biz, body, key, options) => platformApi.validateImport(biz, body, key, options),
        signal,
        // Hallazgo 9: invalidate exactly this query key (never every query
        // in the app), for the businessId the request actually belongs to
        // -- the persisted job is real regardless of what is selected now.
        invalidateImportsCache: (biz) => { void queryClient.invalidateQueries({ queryKey: platformImportsQueryKey(biz, platformApi.source) }); },
        markVerified: (biz) => setVerification({ status: "verified", businessId: biz }),
        markVerificationError: (biz) => setVerification({ status: "error", businessId: biz }),
        setJob,
        setSubmitError,
        setSubmitting,
        isMounted: () => isMountedRef.current,
        contentGuard: contentGuardRef.current,
        contentRequestId,
        submissionGuard: submissionGuardRef.current,
        submissionRequestId,
      },
    );
  }

  async function loadFile(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    const input = event.target;
    if (!file) return;
    // Hallazgo 3 (second independent review): invalidate immediately, not
    // after the read -- a previous job/result/error must disappear the
    // moment a new file is chosen, not only once its (possibly slow) read
    // finishes.
    invalidateEditableContext();
    setFileReadError(null);
    const requestId = fileReadGuardRef.current.bump();
    try {
      await applyFileRead(
        { name: file.name, readText: () => file.text() },
        { isMounted: () => isMountedRef.current, guard: fileReadGuardRef.current, requestId, setFileName, setText, setFileReadError },
      );
    } finally {
      // Cleared whether the read succeeded or failed -- otherwise a failed
      // read leaves the picker unable to re-select the same file.
      input.value = "";
    }
  }

  function downloadJson() {
    if (!result?.rows.length) return;
    const blob = new Blob([JSON.stringify({ schemaVersion: 1, profile: profile.name, records: result.rows }, null, 2)], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement("a");
    anchor.href = url; anchor.download = `${fileName.replace(/\.[^.]+$/, "") || "importacion"}.canonical.json`; anchor.click();
    URL.revokeObjectURL(url);
  }

  return <section className="implementation-page">
    <header className="implementation-hero"><div><p className="eyebrow">OPERACIÓN INTERNA</p><h1>Implementación</h1><p>Prepara los archivos de cada empresa antes de permitir que entren al modelo de cobranza.</p></div><div className="safety-badge"><span aria-hidden="true">✓</span><div><strong>Validar no crea nada</strong><small>Solo «Aplicar» crea clientes y cobros</small></div></div></header>
    <BusinessSelector memberships={memberships} selection={businessSelection} onChange={changeBusiness} resolvedBusinessId={businessId} />
    <nav className="implementation-tabs" aria-label="Secciones de implementación">
      <Tab active={section === "imports"} onClick={() => setSection("imports")} count="01">Importaciones</Tab>
      <Tab active={section === "rules"} onClick={() => setSection("rules")} count="02">Reglas y perfil</Tab>
      <Tab active={section === "businesses"} onClick={() => setSection("businesses")} count="03">Negocios</Tab>
      <Tab active={section === "audit"} onClick={() => setSection("audit")} count="04">Auditoría <span className="tab-count">{filterAuditEntriesForBusiness(audit, businessId).length}</span></Tab>
    </nav>

    {section === "imports" && <div className="implementation-stack">
      <section className="implementation-steps"><Step number="1" title="Fuente" detail="Pega desde Excel o carga CSV/TSV" active /><Step number="2" title="Mapeo" detail="Relaciona columnas con Cauce" active={Boolean(text)} /><Step number="3" title="Validación" detail="Corrige antes de aplicar" active={Boolean(result)} /><Step number="4" title="Aplicación" detail="Explícita, confirmada y auditada" active={job?.status === "validated" || job?.status === "applying" || job?.status === "applied" || job?.status === "review"} /></section>
      <div className="etl-grid">
        <section className="platform-card source-card"><CardHeading label="Archivo de origen" title={fileName} detail={`${parsed.table?.rows.length ?? 0} filas detectadas · ${parsed.table?.headers.length ?? 0} columnas`} />
          <div className="file-actions"><label className="button secondary file-button">Cargar CSV o TSV<input type="file" accept=".csv,.tsv,text/csv,text/tab-separated-values" onChange={(event) => void loadFile(event)} /></label><button className="text-button" onClick={() => { invalidateEditableContext(); setFileReadError(null); setText(sample); setFileName("Datos de ejemplo"); }}>Restaurar ejemplo</button></div>
          <label className="field-label">Pega las celdas copiadas desde Excel<textarea value={text} onChange={(event) => { invalidateEditableContext(); setFileReadError(null); setText(event.target.value); setFileName("Datos pegados desde Excel"); }} spellCheck={false} /></label>
          {parsed.error && <p className="inline-error" role="alert">{parsed.error}</p>}
          {fileReadError && <p className="inline-error" role="alert">{fileReadError}</p>}
          <p className="field-help">Para el piloto aceptamos datos pegados desde Excel y archivos CSV/TSV. Los binarios .xlsx se transformarán en el backend, no en el navegador.</p>
        </section>
        <section className="platform-card mapping-card"><CardHeading label="Contrato canónico" title="Mapeo de columnas" detail="Los identificadores externos permiten reintentos sin duplicar cobros." />
          <div className="mapping-list">{canonicalFields.map((field) => <label key={field}><span>{labels[field]}{field !== "customerEmail" && <b>*</b>}</span><select value={mapping[field] ?? ""} onChange={(event) => { invalidateEditableContext(); setMapping((current) => ({ ...current, [field]: event.target.value || undefined })); }}><option value="">Sin asignar</option>{parsed.table?.headers.map((header) => <option key={header} value={header}>{header}</option>)}</select></label>)}</div>
          <button className="button primary wide" disabled={!parsed.table?.rows.length || submitting || !businessId} onClick={() => void runDryRun()}>{submitting ? "Validando en el servidor…" : "Validar lote"} <span aria-hidden="true">→</span></button>
          {!businessId && <p className="inline-error" role="alert">Selecciona o escribe el ID del negocio destino antes de validar.</p>}
        </section>
      </div>
      {result && <ValidationResult result={result} downloadJson={downloadJson} />}
      {submitting && <p className="field-help" role="status">Enviando el lote al servidor para una validación independiente…</p>}
      {submitError != null && <p className="inline-error" role="alert">{submitErrorMessage(submitError)}</p>}
      {job && <ServerJobResult job={job} />}
      {job && job.businessId === businessId && <ApplyPanel
        key={`${businessId}:${job.importId}`}
        businessId={businessId}
        businessName={businessName}
        initialJob={job}
        preview={job.status !== "purged" && result && result.issues.length === 0 && result.rows.length === job.summary.inputRows ? summarizePreview(result.rows) : undefined}
        onChange={setJob}
      />}
    </div>}

    {section === "rules" && <RulesPanel profile={profile} setProfile={(next) => { invalidateEditableContext(); setProfile(next); }} />}
    {section === "businesses" && <BusinessesPanel businessId={businessId} businessName={businessName} verification={verification} />}
    {section === "audit" && <AuditPanel entries={audit} businessId={businessId} businessName={businessName} />}
  </section>;
}

function Tab({ active, onClick, count, children }: { active: boolean; onClick: () => void; count: string; children: React.ReactNode }) {
  return <button className={active ? "active" : ""} onClick={onClick}><small>{count}</small><span>{children}</span></button>;
}

function Step({ number, title, detail, active }: { number: string; title: string; detail: string; active: boolean }) {
  return <div className={active ? "active" : ""}><span>{number}</span><p><strong>{title}</strong><small>{detail}</small></p></div>;
}

function CardHeading({ label, title, detail }: { label: string; title: string; detail: string }) {
  return <header className="platform-card-heading"><div><p className="eyebrow">{label}</p><h2>{title}</h2></div><small>{detail}</small></header>;
}

const MANUAL_OPTION = "__manual__";

function BusinessSelector({ memberships, selection, onChange, resolvedBusinessId }: {
  memberships: BusinessMembership[]; selection: BusinessSelection; onChange: (next: BusinessSelection) => void; resolvedBusinessId: string;
}) {
  // Hallazgo 3: platform access never implies tenant membership. A
  // superadmin with memberships=[] must still be able to type a
  // businessId manually; one with memberships gets them as a convenience,
  // plus the same manual option for any other business.
  return <section className="platform-card business-selector">
    <CardHeading label="Negocio destino" title="¿Para qué negocio es este lote?" detail="El acceso de plataforma no depende de pertenecer a un negocio." />
    <div className="mapping-list">
      {memberships.length > 0 && <label>
        <span>Negocios disponibles</span>
        <select
          value={selection.mode === "membership" ? selection.membershipId : MANUAL_OPTION}
          onChange={(event) => onChange(event.target.value === MANUAL_OPTION ? { mode: "manual", manualId: "" } : { mode: "membership", membershipId: event.target.value })}
        >
          {memberships.map((membership) => <option key={membership.businessId} value={membership.businessId}>{membership.businessName}</option>)}
          <option value={MANUAL_OPTION}>Otro negocio (escribir ID)…</option>
        </select>
      </label>}
      {(selection.mode === "manual" || memberships.length === 0) && <label>
        <span>ID del negocio {memberships.length === 0 ? "(sin membership disponible)" : "manual"}</span>
        <input
          value={selection.mode === "manual" ? selection.manualId : ""}
          onChange={(event) => onChange({ mode: "manual", manualId: event.target.value })}
          placeholder="id-del-negocio"
        />
      </label>}
    </div>
    {!resolvedBusinessId && <p className="inline-error" role="alert">Escribe o selecciona el ID del negocio destino para continuar. El servidor confirmará que exista.</p>}
  </section>;
}

function serverJobNotice(kind: ReturnType<typeof describeApply>["kind"]): string {
  switch (kind) {
    case "ready": return "Estado validado: todavía no se creó ningún cliente ni cobro. Solo la aplicación explícita de abajo los crea.";
    case "invalid": return "Ningún cliente ni cobro fue creado.";
    case "applied": return "El servidor confirmó que los clientes y cobros de este lote fueron creados.";
    case "review": return "El servidor aplicó el lote, pero hay filas en conflicto que no se crearon.";
    case "in_progress":
    case "resumable": return "La aplicación de este lote está en curso o quedó pendiente en el servidor.";
    case "unverified": return "El servidor reporta este trabajo como aplicado, pero la evidencia no coincide con lo validado. No lo des por bueno.";
    case "purging": return "Se están eliminando los datos temporales de este trabajo.";
    case "purged": return "Los datos temporales de este trabajo fueron eliminados.";
  }
}

function ServerJobResult({ job }: { job: ImportJobView }) {
  if (job.status === "purged") {
    return <section className="validation-result clean"><header><div className="validation-icon" aria-hidden="true">✓</div><div><p className="eyebrow">RESPUESTA DEL SERVIDOR</p><h2>Datos temporales eliminados</h2><p>Trabajo <code>{job.importId}</code> · {serverJobNotice("purged")}</p></div></header></section>;
  }
  const phase = describeApply(job);
  const clean = job.status !== "invalid" && phase.kind !== "unverified";
  const pill = applyStatusPill(job.status);
  const headline = phase.kind === "ready" ? "El servidor validó el lote de forma independiente"
    : phase.kind === "invalid" ? "El servidor encontró problemas en este lote"
    : "Estado del trabajo en el servidor";
  return <section className={`validation-result ${clean ? "clean" : "has-errors"}`}>
    <header><div className="validation-icon" aria-hidden="true">{clean ? "✓" : "!"}</div><div><p className="eyebrow">RESPUESTA DEL SERVIDOR</p><h2>{headline}</h2><p>Trabajo <code>{job.importId}</code> del negocio <code>{job.businessId}</code> · estado <span className={`status-pill ${pill.tone}`}><i className="status-dot" />{pill.label}</span>. {serverJobNotice(phase.kind)}</p></div></header>
    <div className="result-metrics"><Metric label="Filas leídas" value={String(job.summary.inputRows)} /><Metric label="Filas válidas" value={String(job.summary.validRows)} /><Metric label="Con error" value={String(job.summary.errorRows)} danger={job.summary.errorRows > 0} /><Metric label="Monto válido" value={formatMxn(job.summary.totalMinor)} /></div>
    {job.issues.length > 0 && <div className="issue-table"><div className="issue-head"><span>Fila</span><span>Campo</span><span>Qué corregir</span></div>{job.issues.map((issue, index) => <div key={`${issue.row}-${issue.field}-${index}`}><strong>{issue.row}</strong><span>{issue.field}</span><p>{issue.message}</p></div>)}{job.issuesTruncated && <p className="field-help">Se muestran solo los primeros errores; el conteo de arriba es exacto.</p>}</div>}
  </section>;
}

function ValidationResult({ result, downloadJson }: { result: ImportResult; downloadJson: () => void }) {
  const clean = result.issues.length === 0;
  return <section className={`validation-result ${clean ? "clean" : "has-errors"}`}>
    <header><div className="validation-icon" aria-hidden="true">{clean ? "✓" : "!"}</div><div><p className="eyebrow">RESULTADO DEL DRY RUN</p><h2>{clean ? "El lote está listo para la etapa de importación" : "Hay datos que necesitan corrección"}</h2><p>{clean ? "El JSON respeta el contrato canónico. Aún no se ha escrito ningún dato." : "Corrige el archivo o el mapeo y vuelve a validar."}</p></div>{clean && <button className="button secondary" onClick={downloadJson}>Descargar JSON</button>}</header>
    <div className="result-metrics"><Metric label="Filas leídas" value={String(result.summary.inputRows)} /><Metric label="Filas válidas" value={String(result.summary.validRows)} /><Metric label="Con error" value={String(result.summary.errorRows)} danger={result.summary.errorRows > 0} /><Metric label="Monto válido" value={formatMxn(result.summary.totalMinor)} /></div>
    {result.issues.length > 0 && <div className="issue-table"><div className="issue-head"><span>Fila</span><span>Campo</span><span>Qué corregir</span></div>{result.issues.slice(0, 30).map((issue, index) => <div key={`${issue.row}-${issue.field}-${index}`}><strong>{issue.row}</strong><span>{issue.field === "file" ? "Archivo" : labels[issue.field]}</span><p>{issue.message}</p></div>)}</div>}
    {clean && <details className="json-preview"><summary>Ver muestra del JSON canónico</summary><pre>{JSON.stringify(result.rows.slice(0, 2), null, 2)}</pre></details>}
  </section>;
}

function Metric({ label, value, danger }: { label: string; value: string; danger?: boolean }) {
  return <div className={danger ? "danger" : ""}><small>{label}</small><strong>{value}</strong></div>;
}

function RulesPanel({ profile, setProfile }: { profile: ImportProfile; setProfile: (profile: ImportProfile) => void }) {
  const update = <K extends keyof ImportProfile>(key: K, value: ImportProfile[K]) => setProfile({ ...profile, [key]: value });
  return <div className="rules-layout"><section className="platform-card"><CardHeading label="Perfil ETL" title={profile.name} detail="Parámetros aplicados antes de validar cualquier fila." /><div className="profile-form"><label><span>Nombre del perfil</span><input value={profile.name} onChange={(event) => update("name", event.target.value)} /></label><label><span>Separador de columnas</span><select value={profile.delimiter} onChange={(event) => update("delimiter", event.target.value as ImportProfile["delimiter"])}><option value="auto">Detectar automáticamente</option><option value="tab">Tabulador</option><option value="comma">Coma</option><option value="semicolon">Punto y coma</option></select></label><label><span>Formato de fecha</span><select value={profile.dateFormat} onChange={(event) => update("dateFormat", event.target.value as ImportProfile["dateFormat"])}><option value="iso">AAAA-MM-DD</option><option value="dmy">DD/MM/AAAA</option><option value="mdy">MM/DD/AAAA</option></select></label><label><span>Separador decimal</span><select value={profile.decimalSeparator} onChange={(event) => update("decimalSeparator", event.target.value as ImportProfile["decimalSeparator"])}><option value="dot">Punto: 1,250.50</option><option value="comma">Coma: 1.250,50</option></select></label></div></section><section className="platform-card rule-book"><CardHeading label="Guardas del piloto" title="Reglas no negociables" detail="Evitan que una hoja ambigua se convierta en saldo real." /><ul><li><span>01</span><div><strong>Importes en centavos enteros</strong><p>MXN, positivos y con máximo dos decimales.</p></div></li><li><span>02</span><div><strong>Identificadores externos estables</strong><p>Una referencia de cobro no puede repetirse dentro del lote.</p></div></li><li><span>03</span><div><strong>Fechas explícitas</strong><p>El formato seleccionado debe coincidir en todas las filas.</p></div></li><li><span>04</span><div><strong>Sin historial financiero</strong><p>El piloto solo prepara clientes y cobros abiertos; pagos y reembolsos no se infieren.</p></div></li><li><span>05</span><div><strong>Confirmación posterior</strong><p>Validar guarda un trabajo auditable con idempotencia, pero nunca crea clientes ni cobros. Solo «Aplicar», con confirmación explícita y autorización de plataforma en el servidor, los crea; y solo el servidor puede declarar que se aplicó.</p></div></li></ul></section></div>;
}

function BusinessesPanel({ businessId, businessName, verification }: { businessId: string; businessName: string; verification: BusinessVerification }) {
  const platformApi = usePlatformApi();
  // Hallazgo 3 (independent review, 2026-09-14): "which HTTP adapter is
  // configured" and "has this exact businessId actually been confirmed by
  // the server" are two different claims -- a manually-typed, never-
  // submitted businessId used to show the same green "Conectado" pill as
  // one the server had genuinely accepted. Kept as two separate pills:
  // the adapter one is always neutral (informational only), and the
  // verification one is the only thing ever allowed to render success.
  const badge = businessVerificationBadge(businessId, verification);
  return <section className="platform-card businesses-panel"><CardHeading label="Destino del lote" title="Negocio seleccionado" detail="El alta de tenants seguirá siendo una acción separada y auditable." />
    {businessId ? <div className="business-record">
        <span className="business-monogram">NE</span>
        <div><strong>{businessName}</strong><code>{businessId}</code></div>
        <span className="status-pill neutral"><i className="status-dot" />{platformApi.source === "live" ? "API real configurada" : "Fixture activo"}</span>
        <span className={`status-pill ${badge.tone}`}><i className="status-dot" />{badge.label}</span>
      </div>
      : <EmptyState title="Ningún negocio seleccionado" detail="Usa el selector de negocio destino arriba de las pestañas." />}
    <div className="blocked-action"><div><strong>Alta de un negocio nuevo</strong><p>Se habilitará cuando exista el endpoint de plataforma con autorización server-side.</p></div><button className="button secondary" disabled>Crear negocio</button></div>
  </section>;
}

function AuditPanel({ entries, businessId, businessName }: { entries: LocalAuditEntry[]; businessId: string; businessName: string }) {
  // Hallazgo 2 (independent review, 2026-09-14): the full local history is
  // never discarded here -- only filtered for display, so switching back
  // to a previous business still shows its own entries.
  const scoped = filterAuditEntriesForBusiness(entries, businessId);
  return <div className="rules-layout">
    <ServerImportsPanel key={businessId} businessId={businessId} businessName={businessName} />
    <section className="platform-card audit-panel"><CardHeading label="Actividad local" title="Validaciones en este navegador" detail="Todo intento para el negocio seleccionado, incluso los que nunca se enviaron al servidor. Se conserva solo durante esta sesión." />{scoped.length === 0 ? <div className="audit-empty"><span>◎</span><h2>Aún no hay validaciones</h2><p>Ejecuta un dry run para ver aquí su resultado.</p></div> : <div className="audit-list">{scoped.map((entry) => <div key={entry.id}><time>{new Intl.DateTimeFormat("es-MX", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date(entry.at))}</time><strong>{entry.file}</strong><code>{entry.businessName}</code><span className={`status-pill ${entry.result.issues.length ? "danger" : "success"}`}><i className="status-dot" />{entry.result.issues.length ? `${entry.result.summary.errorRows} con error` : `${entry.result.summary.validRows} válidas`}</span></div>)}</div>}</section>
  </div>;
}

function ServerImportsPanel({ businessId, businessName }: { businessId: string; businessName: string }) {
  const platformApi = usePlatformApi();
  const [openId, setOpenId] = useState<string | null>(null);
  // Hallazgo 3: with no business selected, this must never call the API
  // (there is nothing valid to query) and must never look like an
  // indefinite loading state -- TanStack Query's `isPending` stays true
  // forever for a query left permanently `enabled: false`, so that branch
  // is skipped entirely below rather than relying on isPending to resolve.
  const query = useQuery({
    queryKey: platformImportsQueryKey(businessId, platformApi.source),
    queryFn: ({ signal }) => platformApi.listImports(businessId, { limit: 20, signal }),
    enabled: Boolean(businessId),
  });
  // Opening a job always reads it fresh from the server (never the list's
  // copy): apply/resume/purge decisions are made on the server's current state.
  const opened = useQuery({
    queryKey: ["platform-import", businessId, openId, platformApi.source],
    queryFn: ({ signal }) => platformApi.getImport(businessId, openId as string, { signal }),
    enabled: Boolean(businessId && openId),
    staleTime: 0,
    gcTime: 0,
  });
  return <section className="platform-card audit-panel"><CardHeading label="Persistido en el servidor" title="Trabajos de importación" detail="Cada validación exitosa en modo servidor queda aquí, con su identificador, estado y resumen." />
    {!businessId ? <EmptyState title="Ningún negocio seleccionado" detail="Selecciona un negocio destino para ver sus trabajos guardados." />
      : query.isPending ? <LoadingState label="Cargando trabajos guardados…" />
      : query.isError ? <ErrorState error={query.error} retry={() => void query.refetch()} />
      : query.data.items.length === 0 ? <div className="audit-empty"><span>◎</span><h2>Sin trabajos guardados todavía</h2><p>Valida un lote limpio para que el servidor lo registre aquí.</p></div>
      : <div className="audit-list">{query.data.items.map((job) => {
          const pill = applyStatusPill(job.status);
          return <div key={job.importId}><time>{new Intl.DateTimeFormat("es-MX", { hour: "2-digit", minute: "2-digit", second: "2-digit" }).format(new Date(job.createdAt))}</time><strong><code>{job.importId}</code> <button className="text-button" onClick={() => setOpenId(openId === job.importId ? null : job.importId)}>{openId === job.importId ? "Cerrar" : "Abrir"}</button></strong><span className={`status-pill ${pill.tone}`}><i className="status-dot" />{pill.label}</span></div>;
        })}</div>}
    {query.data?.hasMore && <p className="field-help">Hay más trabajos de los que se muestran aquí.</p>}
    {openId && (opened.isPending ? <LoadingState label="Leyendo el trabajo en el servidor…" />
      : opened.isError ? <ErrorState error={opened.error} retry={() => void opened.refetch()} />
      : opened.data === null ? <EmptyState title="Trabajo no encontrado" detail="El servidor no tiene este trabajo para el negocio seleccionado." />
      : <ApplyPanel key={`${businessId}:${openId}`} businessId={businessId} businessName={businessName} initialJob={opened.data} />)}
  </section>;
}
