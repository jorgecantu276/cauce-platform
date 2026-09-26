import { ApiError } from "../api/http";
import type { ApplyProgress, ImportJob, ImportJobPage, ImportJobView, PlatformApi, PurgedImport, ValidateImportInput } from "../api/platform-contracts";

// The single fixture "subject" this dev-mode identity represents (matches
// auth-adapter.ts's FixtureAuthAdapter). Idempotency is still scoped by it
// explicitly, not implicitly assumed, so this stays correct if a fixture
// ever needs to simulate more than one signed-in subject.
const FIXTURE_SUBJECT = "fixture-owner-01";

type StoredEntry = {
  businessId: string; subjectId: string; idempotencyKey: string; payloadKey: string; job: ImportJob;
  /** Dev-only: the real server keeps rows as row chunks; the fixture keeps them in memory. */
  records: ValidateImportInput["records"]; applyKeys: Set<string>;
};

let entries: StoredEntry[] = [];
let purgedImports = new Map<string, PurgedImport>();

/** Mirrors the real server's per-invocation slice (imports.APPLY_ROWS_PER_INVOCATION). */
const FIXTURE_ROWS_PER_APPLY_CALL = 50;
const FIXTURE_RETENTION_DAYS = 30;

function delay<T>(value: T): Promise<T> {
  // Plain setTimeout (not window.setTimeout): this file is exercised both
  // in the browser and by this module's own Node-environment unit tests,
  // and setTimeout is a real global in both -- window is not.
  return new Promise((resolve) => setTimeout(() => resolve(value), 250));
}

/** Mirrors payments/imports.py's `_clean_str`: collapse any run of
 * whitespace to one space and trim. Independent review, 2026-09-14
 * (Hallazgo 4): the fixture previously compared these fields close to raw,
 * so two requests the real backend's `canonical_digest` treats as the
 * exact same payload (incidental spacing, email casing) could disagree
 * here and produce a spurious 409 that would never happen against the
 * real server. */
function collapseWhitespace(value: string | null | undefined): string {
  return (value ?? "").replace(/\s+/g, " ").trim();
}

/** Mirrors payments/imports.py's `_canonical_record` email normalization
 * exactly: strip + lowercase, empty/missing collapses to `null` -- not
 * whitespace-collapsed like other string fields, since `_clean_str` is
 * deliberately not used there either. */
function normalizedEmail(value: string | null | undefined): string | null {
  if (value === null || value === undefined || value === "") return null;
  const cleaned = String(value).trim().toLowerCase();
  return cleaned || null;
}

/** A stable comparison key for "is this the same payload as before",
 * normalized the same way the real backend's `canonical_digest` is (see
 * payments/imports.py's `_clean_str` and email handling) so this fixture
 * agrees with the real server about which requests are equivalent. It does
 * not need to match the backend's digest byte-for-byte -- only to make the
 * same same-payload/different-payload distinction -- since this fixture
 * never talks to the real server. Deliberately picks only the fields the
 * backend's digest itself covers, so an unrecognized extra property (which
 * the backend silently ignores) can never provoke an artificial 409 here
 * either. */
function canonicalPayloadKey(input: ValidateImportInput): string {
  return JSON.stringify({
    source: { fileName: collapseWhitespace(input.source.fileName), format: input.source.format },
    profile: {
      name: collapseWhitespace(input.profile.name),
      delimiter: input.profile.delimiter,
      dateFormat: input.profile.dateFormat,
      decimalSeparator: input.profile.decimalSeparator,
      currency: input.profile.currency,
    },
    records: input.records.map((record) => ({
      sourceRow: record.sourceRow,
      customer: {
        externalId: collapseWhitespace(record.customer.externalId),
        displayName: collapseWhitespace(record.customer.displayName),
        email: normalizedEmail(record.customer.email),
      },
      charge: {
        externalId: collapseWhitespace(record.charge.externalId),
        amountMinor: record.charge.amountMinor,
        currency: record.charge.currency,
        description: collapseWhitespace(record.charge.description),
        dueDate: record.charge.dueDate,
      },
    })),
  });
}

function summarize(input: ValidateImportInput): { status: ImportJob["status"]; summary: ImportJob["summary"] } {
  // The frontend only ever sends rows that already passed its own local
  // dry run, so a fixture batch is always "validated" -- this mirrors the
  // real server's behavior for that same case without re-implementing
  // payments/imports.py's full rule set in TypeScript. Live mode is where
  // an actually independent re-validation happens.
  const totalMinor = input.records.reduce((sum, record) => sum + record.charge.amountMinor, 0);
  return {
    status: "validated",
    summary: { inputRows: input.records.length, validRows: input.records.length, errorRows: 0, totalMinor },
  };
}

export const fixturePlatformApi: PlatformApi = {
  source: "fixture",
  validateImport: async (businessId, input, idempotencyKey) => {
    const payloadKey = canonicalPayloadKey(input);
    // Scoped by (businessId, subjectId, idempotencyKey), matching the real
    // server's contract exactly -- not by idempotencyKey alone.
    const existing = entries.find((entry) => entry.businessId === businessId && entry.subjectId === FIXTURE_SUBJECT && entry.idempotencyKey === idempotencyKey);
    if (existing) {
      if (existing.payloadKey !== payloadKey) throw new ApiError(409, "operation_conflict");
      return delay(existing.job);
    }
    const { status, summary } = summarize(input);
    const now = new Date().toISOString();
    const job: ImportJob = {
      importId: `fixture-import-${crypto.randomUUID()}`,
      businessId, status, source: input.source, profile: input.profile, summary,
      issues: [], issuesTruncated: false, createdAt: now, validatedAt: now, createdBySubject: FIXTURE_SUBJECT,
      rowsExpireAt: new Date(Date.now() + FIXTURE_RETENTION_DAYS * 86_400_000).toISOString(), apply: null,
    };
    entries = [{ businessId, subjectId: FIXTURE_SUBJECT, idempotencyKey, payloadKey, job, records: input.records, applyKeys: new Set() }, ...entries];
    return delay(job);
  },
  getImport: async (businessId, importId): Promise<ImportJobView | null> => delay(
    entries.find((entry) => entry.businessId === businessId && entry.job.importId === importId)?.job
      ?? purgedImports.get(`${businessId}:${importId}`) ?? null,
  ),
  applyImport: async (businessId, importId, idempotencyKey): Promise<ImportJobView> => {
    const entry = entries.find((candidate) => candidate.businessId === businessId && candidate.job.importId === importId);
    if (!entry) {
      // A purged job and a job of another business are told apart exactly as the server does.
      if (purgedImports.has(`${businessId}:${importId}`)) throw new ApiError(409, "import_not_applicable");
      throw new ApiError(404, "not_found");
    }
    const { status } = entry.job;
    if (status === "applied" || status === "review") {
      if (entry.applyKeys.has(idempotencyKey)) return delay(entry.job);
      throw new ApiError(409, "import_not_applicable");
    }
    if (status !== "validated" && status !== "applying") throw new ApiError(409, "import_not_applicable");
    entry.applyKeys.add(idempotencyKey);
    const total = entry.job.summary.validRows;
    const processed = Math.min(total, (entry.job.apply?.rowsProcessed ?? 0) + FIXTURE_ROWS_PER_APPLY_CALL);
    const done = processed === total;
    const decided = entry.records.slice(0, processed);
    const customers = new Set(decided.map((record) => record.customer.externalId)).size;
    const now = new Date().toISOString();
    const apply: ApplyProgress = {
      rowsTotal: total, rowsProcessed: processed, customersCreated: customers, customersReused: processed - customers,
      chargesCreated: processed, chargesReused: 0, conflicts: 0, failed: 0, leaseActive: false, lastErrorCode: null,
      startedAt: entry.job.apply?.startedAt ?? now, completedAt: done ? now : null, conflictSample: [], conflictsTruncated: false,
    };
    entry.job = { ...entry.job, status: done ? "applied" : "applying", apply };
    return delay(entry.job);
  },
  purgeImport: async (businessId, importId): Promise<PurgedImport> => {
    const already = purgedImports.get(`${businessId}:${importId}`);
    if (already) return delay(already);
    const entry = entries.find((candidate) => candidate.businessId === businessId && candidate.job.importId === importId);
    if (!entry) throw new ApiError(404, "not_found");
    const purged: PurgedImport = {
      importId, businessId, status: "purged", purgedAt: new Date().toISOString(),
      purge: { priorStatus: entry.job.status, chunksDeleted: Math.ceil(entry.records.length / 25), rowResultsDeleted: entry.job.apply?.rowsProcessed ?? 0, applyGuardsDeleted: entry.applyKeys.size },
    };
    entries = entries.filter((candidate) => candidate !== entry);
    purgedImports.set(`${businessId}:${importId}`, purged);
    return delay(purged);
  },
  listImports: async (businessId, options) => {
    const limit = options?.limit ?? 50;
    const scoped = entries.filter((entry) => entry.businessId === businessId).map((entry) => entry.job);
    return delay<ImportJobPage>({ items: scoped.slice(0, limit), hasMore: scoped.length > limit, cursor: null });
  },
};

/** Test-only: this module's state is otherwise a module-level singleton, so
 * without an explicit reset one test's fixture jobs would leak into the
 * next. Not used by application code. */
export function resetFixturePlatformState(): void {
  entries = [];
  purgedImports = new Map();
}
