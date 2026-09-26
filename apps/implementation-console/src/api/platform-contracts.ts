import type { CanonicalImportRow, ImportProfile } from "../etl/import-engine";
import type { RequestOptions } from "./http";

/** The wire shape for one record: everything import-engine.ts already
 * produces except the frontend-only `businessId` convenience field -- the
 * server takes businessId from the URL path, never from the body. */
export type ImportRecordInput = Omit<CanonicalImportRow, "businessId">;

export type ImportSource = { fileName: string; format: "csv" | "tsv" | "paste" };

export type ImportIssue = { row: number; field: string; message: string };

export type ImportSummary = { inputRows: number; validRows: number; errorRows: number; totalMinor: number };

/** Mirrors backend payments/imports.IMPORT_STATES. `validated` only means
 * the batch satisfies the input contract; only `applied` (or `review`, with
 * its conflicts) means Customers/Charges were created -- and only the server
 * ever says so. `purged` is not a stored state: a purged job's metadata is
 * gone, so it is read back as PurgedImport. */
export type ImportJobStatus = "validated" | "invalid" | "applying" | "applied" | "review" | "purging";

/** Server-derived apply progress (backend DynamoRepository._apply_progress).
 * Every number comes from persisted per-row results, never from the client. */
export type ApplyProgress = {
  rowsTotal: number;
  rowsProcessed: number;
  customersCreated: number;
  customersReused: number;
  chargesCreated: number;
  chargesReused: number;
  conflicts: number;
  /** 1 when the last slice stopped on a recoverable error (see lastErrorCode), else 0. */
  failed: number;
  /** True while some invocation holds the apply lease and is processing. */
  leaseActive: boolean;
  lastErrorCode: string | null;
  startedAt: string | null;
  completedAt: string | null;
  /** Row number + fixed reason code only; never personal data. */
  conflictSample: Array<{ sourceRow: number; code: string }>;
  conflictsTruncated: boolean;
};

export type ImportJob = {
  importId: string;
  businessId: string;
  status: ImportJobStatus;
  source: ImportSource;
  profile: ImportProfile;
  summary: ImportSummary;
  issues: ImportIssue[];
  issuesTruncated: boolean;
  createdAt: string;
  validatedAt: string | null;
  createdBySubject: string;
  /** When the temporary row data (the only personal data) expires. */
  rowsExpireAt: string | null;
  apply: ApplyProgress | null;
};

/** What a job reads as after its temporary data was purged: only minimal,
 * PII-free evidence remains on the server. */
export type PurgedImport = {
  importId: string;
  businessId: string;
  status: "purged";
  purgedAt: string;
  purge: { priorStatus: string | null; chunksDeleted: number; rowResultsDeleted: number; applyGuardsDeleted: number };
};

export type ImportJobView = ImportJob | PurgedImport;

export type ImportJobPage = { items: ImportJob[]; hasMore: boolean; cursor: string | null };

export type ValidateImportInput = {
  source: ImportSource;
  profile: ImportProfile;
  records: ImportRecordInput[];
};

export interface PlatformApi {
  readonly source: "live" | "fixture";
  validateImport(businessId: string, input: ValidateImportInput, idempotencyKey: string, options?: RequestOptions): Promise<ImportJob>;
  getImport(businessId: string, importId: string, options?: RequestOptions): Promise<ImportJobView | null>;
  /** Runs ONE bounded slice of the server-side apply. Call again with the
   * SAME idempotency key until the returned status is final. The only source
   * of "applied": the UI must not infer it. */
  applyImport(businessId: string, importId: string, idempotencyKey: string, options?: RequestOptions): Promise<ImportJobView>;
  purgeImport(businessId: string, importId: string, idempotencyKey: string, options?: RequestOptions): Promise<PurgedImport>;
  listImports(businessId: string, options?: { limit?: number; cursor?: string | null } & RequestOptions): Promise<ImportJobPage>;
}
