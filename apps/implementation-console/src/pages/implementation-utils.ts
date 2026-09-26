import type { HeaderMapping, ImportProfile, CanonicalField, ImportResult } from "../etl/import-engine";
import type { ImportJob, ImportRecordInput, ImportSource } from "../api/platform-contracts";
import { getOrCreateOperation, newKey } from "../lib/idempotency";
import { isRequestCancelled, type RequestOptions } from "../api/http";

// --- Hallazgo 6: a deterministic signature of the exact payload that will
// actually be sent, used only to key a stable Idempotency-Key across
// retries of the *same* batch (see lib/idempotency.ts). No secret is
// involved -- this exists purely to avoid accidental collisions between
// batches that are meaningfully different, so a real hash (SHA-256 via Web
// Crypto, always available in browsers and in Node 18+) is used instead of
// a 32-bit rolling hash.

export function sourceFormat(fileName: string): ImportSource["format"] {
  if (/\.tsv$/i.test(fileName)) return "tsv";
  if (/\.csv$/i.test(fileName)) return "csv";
  return "paste";
}

async function sha256Hex(value: string): Promise<string> {
  const bytes = new TextEncoder().encode(value);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest)).map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

export type BatchSignatureInput = {
  businessId: string;
  source: ImportSource;
  profile: ImportProfile;
  mapping: HeaderMapping;
  /** The exact pasted/loaded text. Hashing this (the policy already in
   * place before this fix) transitively covers any content change, since
   * mapping+profile+text together fully determine the records that will be
   * built from it -- no need to separately serialize `records`. */
  text: string;
};

/** Every field that actually varies the request the server will see,
 * canonicalized so accidental differences (mapping key insertion order)
 * never change the signature, and every meaningful difference (file name,
 * source format, any profile option, mapping, content, or destination
 * business) always does. */
export async function batchSignature(input: BatchSignatureInput): Promise<string> {
  const canonicalMapping = Object.keys(input.mapping).sort()
    .map((key) => `${key}=${input.mapping[key as CanonicalField] ?? ""}`)
    .join("&");
  const parts = [
    input.businessId,
    input.source.fileName, input.source.format,
    input.profile.name, input.profile.delimiter, input.profile.dateFormat, input.profile.decimalSeparator, input.profile.currency,
    canonicalMapping,
    input.text,
  ];
  return sha256Hex(parts.join(""));
}

// --- Hallazgo 3: a superadmin's platform access is independent of tenant
// membership, so the console must let one pick (or manually type) a
// destination business even with memberships=[]. No global business list
// here -- that would need a Scan or a new GSI, explicitly out of scope for
// this slice; the server still verifies the businessId exists regardless
// of how it was chosen.

export type BusinessMembership = { businessId: string; businessName: string };

export type BusinessSelection =
  | { mode: "membership"; membershipId: string }
  | { mode: "manual"; manualId: string };

export function defaultBusinessSelection(memberships: BusinessMembership[]): BusinessSelection {
  return memberships.length > 0
    ? { mode: "membership", membershipId: memberships[0].businessId }
    : { mode: "manual", manualId: "" };
}

/** The businessId actually used by the rest of the page for this
 * selection, or "" when nothing usable is selected yet (an empty manual
 * entry, or a stale membershipId no longer present). Never throws. */
export function resolveBusinessId(memberships: BusinessMembership[], selection: BusinessSelection): string {
  if (selection.mode === "manual") return selection.manualId.trim();
  return memberships.some((membership) => membership.businessId === selection.membershipId) ? selection.membershipId : "";
}

// --- Hallazgo 9: submit and the Auditoría panel must invalidate/read
// *exactly* the same TanStack Query key, or a successful submit can leave
// the panel showing a stale list until staleTime elapses.

export function platformImportsQueryKey(businessId: string, source: string): readonly [string, string, string] {
  return ["platform-imports", businessId, source] as const;
}

// --- Hallazgo 1 (independent review, 2026-09-14): a validate-import
// request is asynchronous, and nothing about the page's own layout
// prevents the user from switching business, loading a different file, or
// editing the mapping/profile while it is in flight. Previously the
// response's callback (setJob/setSubmitError) always wrote to whatever
// state the page had by the time it resolved -- so a response for
// business A could land, and be shown, after the user had already moved
// on to business B. `SubmissionGuard` is a plain monotonic generation
// counter: every UI change that should invalidate an in-flight
// submission's eventual effect on state bumps it, and a submission only
// ever applies its own state updates if the generation is still exactly
// what it captured when it started. The server-side response itself is
// still real and already persisted -- only *showing* it under the wrong
// context is the bug, so the query-cache invalidation for the request's
// own (captured) businessId always runs, regardless of staleness.

// --- P0 (independent review, 2026-09-14): main.tsx renders the app inside
// <StrictMode>, which in development intentionally double-invokes each
// effect (setup -> cleanup -> setup) once on initial mount, to surface
// exactly this class of bug. `useEffect(() => () => { ref.current = false;
// }, [])` has an empty *setup* -- only its cleanup does anything -- so
// after that double-invoke the ref is left `false` permanently, even
// though the component is genuinely mounted: setup1 (no-op) -> cleanup1
// (sets false) -> setup2 (no-op, does not reset it). `trackMount` fixes
// this by giving setup its own job: mark mounted true every time it runs,
// so the *second* setup call correctly restores `true`.
export function trackMount(ref: { current: boolean }): () => void {
  ref.current = true;
  return () => {
    ref.current = false;
  };
}

export type SubmissionGuard = {
  /** Call on any change that should invalidate whatever submission is
   * currently in flight (switching business, loading a new file, editing
   * text/mapping/profile) -- and once at the start of a new submission,
   * whose return value that submission must keep as its own identity. */
  bump(): number;
  /** The current generation, to compare a submission's captured id against. */
  current(): number;
};

export function createSubmissionGuard(): SubmissionGuard {
  let generation = 0;
  return {
    bump: () => (generation += 1),
    current: () => generation,
  };
}

/** Owns the AbortController of the one validate-import request that may be
 * in flight. Any context change that makes that request irrelevant (another
 * business, file, mapping, profile, or the page unmounting) calls `cancel()`;
 * starting a new request also cancels the previous one, so at most one is
 * ever live. Cancelling is intentional and silent -- the transport reports
 * it as RequestCancelledError, which submitValidationBatch never surfaces as
 * an error. */
export type RequestScope = {
  /** Aborts whatever is in flight and returns the signal for the new request. */
  begin(): AbortSignal;
  cancel(): void;
};

export function createRequestScope(): RequestScope {
  let controller: AbortController | null = null;
  return {
    begin: () => {
      controller?.abort();
      controller = new AbortController();
      return controller.signal;
    },
    cancel: () => {
      controller?.abort();
      controller = null;
    },
  };
}

export type ValidationSubmitRequest = {
  businessId: string;
  source: ImportSource;
  profile: ImportProfile;
  mapping: HeaderMapping;
  text: string;
  records: ImportRecordInput[];
};

export type ValidationSubmitDeps = {
  validateImport: (businessId: string, body: { source: ImportSource; profile: ImportProfile; records: ImportRecordInput[] }, idempotencyKey: string, options?: RequestOptions) => Promise<ImportJob>;
  /** Cancelled by the page (see RequestScope) when this request stops being
   * the one on screen. Optional so callers/tests without cancellation work. */
  signal?: AbortSignal;
  /** Always called with the request's own businessId, regardless of
   * whether the submission is still current -- the persisted job exists
   * for that business either way, and its Auditoría cache must reflect it. */
  invalidateImportsCache: (businessId: string) => void;
  /** Verification tracking (Hallazgo 3) is keyed by the request's own
   * businessId, so -- unlike setJob/setSubmitError -- it is safe to call
   * regardless of *content* staleness: it can never misattribute an
   * outcome to whatever business happens to be selected by the time this
   * resolves. It must still never run after unmount, though (P2,
   * independent review, 2026-09-14) -- these do write React state, unlike
   * invalidateImportsCache below. */
  markVerified: (businessId: string) => void;
  markVerificationError: (businessId: string) => void;
  setJob: (job: ImportJob) => void;
  setSubmitError: (error: unknown) => void;
  setSubmitting: (submitting: boolean) => void;
  isMounted: () => boolean;
  /** Gates setJob/setSubmitError. Bumped by switching business, loading a
   * new file, or editing text/mapping/profile -- as well as by starting a
   * new submission -- so any of those invalidate a still-in-flight
   * response's effect on "what is currently on screen". */
  contentGuard: SubmissionGuard;
  contentRequestId: number;
  /** Gates setSubmitting(false), deliberately kept separate from
   * contentGuard: it must be bumped *only* by an actual new submission
   * starting, never by an unrelated content change. Otherwise a stale
   * response's `finally` would correctly skip setJob/setSubmitError but
   * also skip turning the submitting indicator off -- and since nothing
   * else would ever clear it, "Validando en el servidor..." would stay
   * stuck forever the moment business/file/mapping/profile changed while
   * a request was still in flight. */
  submissionGuard: SubmissionGuard;
  submissionRequestId: number;
};

/** Runs one validate-import submission end to end -- the idempotency
 * signature, the request itself, and applying its outcome:
 * - `invalidateImportsCache` always runs, even after unmount -- it only
 *   invalidates the query cache for the request's own businessId, never
 *   writes React state.
 * - `markVerified`/`markVerificationError` run whenever the component is
 *   still mounted, regardless of *content* staleness -- a stale response
 *   still proves (or disproves) that its own businessId is reachable.
 * - `setJob`/`setSubmitError` only run if this is still both mounted and
 *   the current content generation.
 * - `setSubmitting(false)` only runs if no newer submission has since
 *   superseded this one (its own, separate generation). */
export async function submitValidationBatch(request: ValidationSubmitRequest, deps: ValidationSubmitDeps): Promise<void> {
  const isCurrentContent = () => deps.isMounted() && deps.contentGuard.current() === deps.contentRequestId;
  const isCurrentSubmission = () => deps.isMounted() && deps.submissionGuard.current() === deps.submissionRequestId;
  try {
    const signature = await batchSignature({ businessId: request.businessId, source: request.source, profile: request.profile, mapping: request.mapping, text: request.text });
    const operation = getOrCreateOperation("validate-import", signature, () => ({ key: newKey("import-validate") }));
    const submitted = await deps.validateImport(request.businessId, { source: request.source, profile: request.profile, records: request.records }, operation.key, { signal: deps.signal });
    deps.invalidateImportsCache(request.businessId);
    if (deps.isMounted()) deps.markVerified(request.businessId);
    if (isCurrentContent()) deps.setJob(submitted);
  } catch (error) {
    if (isRequestCancelled(error)) {
      // Intentional, not a failure: the page moved on (business/file/edit/
      // unmount). It is never an error to show or a verification verdict.
      // The server may have received the request before it was cancelled,
      // so the Auditoría cache for its own business is still refreshed; the
      // Idempotency-Key it used stays registered, so re-sending the same
      // batch later is a safe retry of the same operation.
      deps.invalidateImportsCache(request.businessId);
    } else {
      if (deps.isMounted()) deps.markVerificationError(request.businessId);
      if (isCurrentContent()) deps.setSubmitError(error);
    }
  } finally {
    if (isCurrentSubmission()) deps.setSubmitting(false);
  }
}

// --- Hallazgo 3, second independent review (2026-09-14): reading an
// uploaded file's text is itself asynchronous (`File.text()`), and nothing
// stopped the user from selecting a different file before the previous
// read resolved -- so an earlier, now-stale read finishing late could
// overwrite a newer selection's fileName/text, or mix one file's name with
// another's content. This uses its own dedicated SubmissionGuard,
// independent of the content/submission guards used for validate-import
// requests, so a stale file read is judged purely against other file
// reads.

export type FileReadRequest = {
  name: string;
  /** Injected so tests can control resolution order with deferred
   * promises instead of real File objects -- in production this is just
   * `() => file.text()`. */
  readText: () => Promise<string>;
};

export type FileReadDeps = {
  isMounted: () => boolean;
  guard: SubmissionGuard;
  requestId: number;
  /** fileName and text are only ever applied together, from the same
   * resolved read -- never one without the other. */
  setFileName: (name: string) => void;
  setText: (text: string) => void;
  setFileReadError: (message: string | null) => void;
};

export async function applyFileRead(request: FileReadRequest, deps: FileReadDeps): Promise<void> {
  const isCurrent = () => deps.isMounted() && deps.guard.current() === deps.requestId;
  try {
    const text = await request.readText();
    if (isCurrent()) {
      deps.setFileName(request.name);
      deps.setText(text);
    }
  } catch (error) {
    if (isCurrent()) {
      deps.setFileReadError(error instanceof Error ? error.message : "No se pudo leer el archivo.");
    }
  }
}

// --- Hallazgo 2 (independent review, 2026-09-14): the local (client-only)
// audit trail previously stored no businessId, so switching business left
// Auditoría showing every prior business's local attempts mixed together
// with no indication which was which. Every entry now carries the
// businessId (and businessName, when known) captured directly from
// whatever was selected at the moment the dry run ran -- never inferred
// from `result.rows[0].businessId`, which would be undefined for an
// invalid batch with zero rows.

export type LocalAuditEntry = { id: string; at: string; businessId: string; businessName: string; file: string; result: ImportResult };

/** The underlying history is never destroyed on a business switch -- only
 * its presentation is scoped, so other businesses' entries are still
 * there if the user switches back. */
export function filterAuditEntriesForBusiness(entries: LocalAuditEntry[], businessId: string): LocalAuditEntry[] {
  return entries.filter((entry) => entry.businessId === businessId);
}

// --- P3, second independent review (2026-09-14): the local audit list was
// capped at 10 entries *globally* (`slice(0, 10)`), so validating a few
// batches for business B could push business A's own recent history clean
// out of the array -- even though A's entries were never meant to compete
// with B's for the same 10 slots. The cap is now per business: appending a
// new entry only ever trims *that business's own* oldest entries past the
// limit, leaving every other business's entries completely untouched.

const DEFAULT_AUDIT_ENTRIES_PER_BUSINESS = 10;

/** Appends one new local audit entry, keeping at most `limitPerBusiness`
 * entries *for that entry's businessId* and leaving every other
 * business's entries as they were. Assigns its own `id` (never derived
 * from `at`) so two entries created in the same millisecond -- entirely
 * possible for rapid consecutive submissions, and routine in tests --
 * still get distinct React keys instead of colliding on a timestamp
 * string. */
export function appendAuditEntry(entries: LocalAuditEntry[], entry: Omit<LocalAuditEntry, "id">, limitPerBusiness = DEFAULT_AUDIT_ENTRIES_PER_BUSINESS): LocalAuditEntry[] {
  const withId: LocalAuditEntry = { ...entry, id: crypto.randomUUID() };
  const sameBusiness = [withId, ...entries.filter((existing) => existing.businessId === entry.businessId)].slice(0, limitPerBusiness);
  const otherBusinesses = entries.filter((existing) => existing.businessId !== entry.businessId);
  return [...sameBusiness, ...otherBusinesses];
}

// --- Hallazgo 3 (independent review, 2026-09-14): the "Conectado" badge
// previously just meant `platformApi.source === "live"` -- i.e. "a real
// HTTP client is configured" -- which is true even for a manually-typed
// businessId nobody has confirmed exists. That is a materially different
// claim from "the server has actually accepted a request for this exact
// business". These are kept as two separate signals: which adapter is
// configured (informational, always neutral tone) and whether *this*
// businessId has actually round-tripped through the server successfully
// (the only thing allowed to render as a green "verified" state).

export type BusinessVerification =
  | { status: "unverified" }
  | { status: "verified"; businessId: string }
  | { status: "error"; businessId: string };

export type BusinessVerificationBadge = { tone: "neutral" | "success" | "danger"; label: string };

/** Success requires the *verified* businessId to equal the one currently
 * selected -- so switching to a different (or not-yet-checked) business
 * never keeps showing a stale green badge earned by a previous one. An
 * error never renders success, for any reason (network failure, the
 * documented ambiguous-400 business-not-found policy, or anything else):
 * the only path to "verified" is an actual successful response for this
 * exact businessId. */
export function businessVerificationBadge(businessId: string, verification: BusinessVerification): BusinessVerificationBadge {
  if (verification.status === "verified" && verification.businessId === businessId) {
    return { tone: "success", label: "Negocio verificado" };
  }
  if (verification.status === "error" && verification.businessId === businessId) {
    return { tone: "danger", label: "El servidor no confirmó este negocio en el último intento" };
  }
  return { tone: "neutral", label: "Pendiente de verificación" };
}
