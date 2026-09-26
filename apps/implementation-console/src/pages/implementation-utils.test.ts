import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  appendAuditEntry, applyFileRead, batchSignature, businessVerificationBadge, createRequestScope, createSubmissionGuard,
  defaultBusinessSelection, filterAuditEntriesForBusiness, platformImportsQueryKey, resolveBusinessId, sourceFormat,
  submitValidationBatch, trackMount,
  type BusinessMembership, type LocalAuditEntry,
} from "./implementation-utils";
import type { ImportProfile, ImportResult } from "../etl/import-engine";
import type { ImportJob } from "../api/platform-contracts";
import { ApiError, RequestCancelledError } from "../api/http";

const profile: ImportProfile = { name: "Cobranza estándar MX", delimiter: "comma", dateFormat: "iso", decimalSeparator: "dot", currency: "MXN" };
const baseInput = {
  businessId: "business-1",
  source: { fileName: "cartera.csv", format: "csv" as const },
  profile,
  mapping: { customerExternalId: "Clave", customerName: "Nombre" },
  text: "Clave,Nombre\nCLI-1,Uno",
};

describe("sourceFormat", () => {
  it("recognizes .tsv and .csv extensions and falls back to paste", () => {
    expect(sourceFormat("cartera.tsv")).toBe("tsv");
    expect(sourceFormat("cartera.csv")).toBe("csv");
    expect(sourceFormat("Datos pegados desde Excel")).toBe("paste");
  });
});

describe("batchSignature (Hallazgo 6)", () => {
  it("produces the same signature for the exact same input", async () => {
    const first = await batchSignature(baseInput);
    const second = await batchSignature(baseInput);
    expect(first).toBe(second);
  });

  it("changes when fileName changes", async () => {
    const base = await batchSignature(baseInput);
    const changed = await batchSignature({ ...baseInput, source: { ...baseInput.source, fileName: "otra-cartera.csv" } });
    expect(changed).not.toBe(base);
  });

  it("changes when the source format changes (csv vs tsv)", async () => {
    const base = await batchSignature(baseInput);
    const changed = await batchSignature({ ...baseInput, source: { ...baseInput.source, format: "tsv" } });
    expect(changed).not.toBe(base);
  });

  it("changes when businessId changes", async () => {
    const base = await batchSignature(baseInput);
    const changed = await batchSignature({ ...baseInput, businessId: "business-2" });
    expect(changed).not.toBe(base);
  });

  it("changes when the mapping changes", async () => {
    const base = await batchSignature(baseInput);
    const changed = await batchSignature({ ...baseInput, mapping: { ...baseInput.mapping, amount: "Monto" } });
    expect(changed).not.toBe(base);
  });

  it("is the same when the same mapping is built with keys inserted in a different order", async () => {
    const mappingA = { customerExternalId: "Clave", customerName: "Nombre", amount: "Monto" };
    const mappingB = { amount: "Monto", customerName: "Nombre", customerExternalId: "Clave" };
    const first = await batchSignature({ ...baseInput, mapping: mappingA });
    const second = await batchSignature({ ...baseInput, mapping: mappingB });
    expect(first).toBe(second);
  });

  it("changes when the profile (e.g. currency or delimiter) changes", async () => {
    const base = await batchSignature(baseInput);
    const changedDelimiter = await batchSignature({ ...baseInput, profile: { ...profile, delimiter: "semicolon" } });
    expect(changedDelimiter).not.toBe(base);
  });

  it("changes when the pasted/loaded text content changes", async () => {
    const base = await batchSignature(baseInput);
    const changed = await batchSignature({ ...baseInput, text: baseInput.text + "\nCLI-2,Dos" });
    expect(changed).not.toBe(base);
  });
});

describe("business selection (Hallazgo 3)", () => {
  const memberships: BusinessMembership[] = [{ businessId: "member-biz-1", businessName: "Negocio Uno" }];

  it("defaults to the first membership when memberships exist", () => {
    expect(defaultBusinessSelection(memberships)).toEqual({ mode: "membership", membershipId: "member-biz-1" });
    expect(resolveBusinessId(memberships, defaultBusinessSelection(memberships))).toBe("member-biz-1");
  });

  it("defaults to an empty manual entry for a superadmin with no memberships", () => {
    const selection = defaultBusinessSelection([]);
    expect(selection).toEqual({ mode: "manual", manualId: "" });
    expect(resolveBusinessId([], selection)).toBe("");
  });

  it("resolves a manually typed businessId even with no memberships available", () => {
    const selection = { mode: "manual" as const, manualId: "  hand-typed-business-id  " };
    expect(resolveBusinessId([], selection)).toBe("hand-typed-business-id");
  });

  it("trims whitespace from a manual entry", () => {
    expect(resolveBusinessId(memberships, { mode: "manual", manualId: "  spaced  " })).toBe("spaced");
  });

  it("resolves to empty when the selected membershipId no longer exists", () => {
    expect(resolveBusinessId(memberships, { mode: "membership", membershipId: "stale-id" })).toBe("");
  });

  it("switching the underlying businessId changes the idempotency signature", async () => {
    const first = await batchSignature({ ...baseInput, businessId: resolveBusinessId(memberships, { mode: "membership", membershipId: "member-biz-1" }) });
    const second = await batchSignature({ ...baseInput, businessId: resolveBusinessId([], { mode: "manual", manualId: "another-biz" }) });
    expect(first).not.toBe(second);
  });
});

describe("platformImportsQueryKey (Hallazgo 9)", () => {
  it("is deterministic for the same business and source", () => {
    expect(platformImportsQueryKey("business-1", "live")).toEqual(["platform-imports", "business-1", "live"]);
  });

  it("differs when the business changes, so switching business never shows the previous business's cache", () => {
    expect(platformImportsQueryKey("business-1", "live")).not.toEqual(platformImportsQueryKey("business-2", "live"));
  });
});

describe("createSubmissionGuard", () => {
  it("starts at generation 0 and increments on every bump", () => {
    const guard = createSubmissionGuard();
    expect(guard.current()).toBe(0);
    expect(guard.bump()).toBe(1);
    expect(guard.bump()).toBe(2);
    expect(guard.current()).toBe(2);
  });
});

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => { resolve = res; reject = rej; });
  // Node flags a promise as "unhandled" the instant it rejects, even if a
  // consumer attaches its own .catch a microtask later (exactly what
  // submitValidationBatch's internal `await` does here) -- this silent
  // catch exists only to keep that harmless timing gap from failing the
  // test run; it has no effect on what the real assertions observe.
  promise.catch(() => {});
  return { promise, resolve, reject };
}

function fakeJob(businessId: string): ImportJob {
  return {
    importId: `import-${businessId}`, businessId, status: "validated",
    source: { fileName: "cartera.csv", format: "csv" },
    profile: { name: "p", delimiter: "comma", dateFormat: "iso", decimalSeparator: "dot", currency: "MXN" },
    summary: { inputRows: 1, validRows: 1, errorRows: 0, totalMinor: 100 },
    issues: [], issuesTruncated: false,
    createdAt: "2026-09-14T12:00:00.000Z", validatedAt: "2026-09-14T12:00:00.000Z", createdBySubject: "subject-1", rowsExpireAt: null, apply: null,
  };
}

describe("submitValidationBatch (Hallazgo 1: stale async responses never win)", () => {
  it("does not apply a stale response's job to a context that has since switched business, but still invalidates the original business's cache", async () => {
    const { promise, resolve } = deferred<ImportJob>();
    const contentGuard = createSubmissionGuard();
    const submissionGuard = createSubmissionGuard();
    const setJob = vi.fn();
    const setSubmitError = vi.fn();
    const setSubmitting = vi.fn();
    const invalidateImportsCache = vi.fn();
    const markVerified = vi.fn();
    const markVerificationError = vi.fn();

    const contentRequestId = contentGuard.bump();
    const submissionRequestId = submissionGuard.bump();
    const run = submitValidationBatch(
      { businessId: "business-A", source: { fileName: "a.csv", format: "csv" }, profile, mapping: {}, text: "x", records: [] },
      {
        validateImport: () => promise,
        invalidateImportsCache, markVerified, markVerificationError, setJob, setSubmitError, setSubmitting,
        isMounted: () => true,
        contentGuard, contentRequestId, submissionGuard, submissionRequestId,
      },
    );

    // The user switches to business B before A's response arrives -- this
    // is exactly what changeBusiness() does in implementation-page.tsx.
    contentGuard.bump();

    resolve(fakeJob("business-A"));
    await run;

    expect(setJob).not.toHaveBeenCalled();
    expect(setSubmitError).not.toHaveBeenCalled();
    expect(invalidateImportsCache).toHaveBeenCalledWith("business-A");
    expect(markVerified).toHaveBeenCalledWith("business-A");
    // The submitting indicator is not left stuck just because content
    // became stale -- nothing else bumped submissionGuard.
    expect(setSubmitting).toHaveBeenCalledWith(false);
  });

  it("discards a stale response the same way when the mapping/profile changes mid-flight, not only on a business switch", async () => {
    const { promise, resolve } = deferred<ImportJob>();
    const contentGuard = createSubmissionGuard();
    const submissionGuard = createSubmissionGuard();
    const setJob = vi.fn();

    const contentRequestId = contentGuard.bump();
    const submissionRequestId = submissionGuard.bump();
    const run = submitValidationBatch(
      { businessId: "business-A", source: { fileName: "a.csv", format: "csv" }, profile, mapping: {}, text: "x", records: [] },
      {
        validateImport: () => promise,
        invalidateImportsCache: vi.fn(), markVerified: vi.fn(), markVerificationError: vi.fn(),
        setJob, setSubmitError: vi.fn(), setSubmitting: vi.fn(),
        isMounted: () => true,
        contentGuard, contentRequestId, submissionGuard, submissionRequestId,
      },
    );

    // Editing the mapping mid-flight bumps the same contentGuard that
    // changeBusiness bumps in the real page.
    contentGuard.bump();
    resolve(fakeJob("business-A"));
    await run;

    expect(setJob).not.toHaveBeenCalled();
  });

  it("applies a still-current response normally", async () => {
    const { promise, resolve } = deferred<ImportJob>();
    const contentGuard = createSubmissionGuard();
    const submissionGuard = createSubmissionGuard();
    const setJob = vi.fn();
    const setSubmitting = vi.fn();

    const contentRequestId = contentGuard.bump();
    const submissionRequestId = submissionGuard.bump();
    const run = submitValidationBatch(
      { businessId: "business-A", source: { fileName: "a.csv", format: "csv" }, profile, mapping: {}, text: "x", records: [] },
      {
        validateImport: () => promise,
        invalidateImportsCache: vi.fn(), markVerified: vi.fn(), markVerificationError: vi.fn(),
        setJob, setSubmitError: vi.fn(), setSubmitting,
        isMounted: () => true,
        contentGuard, contentRequestId, submissionGuard, submissionRequestId,
      },
    );

    resolve(fakeJob("business-A"));
    await run;

    expect(setJob).toHaveBeenCalledWith(fakeJob("business-A"));
    expect(setSubmitting).toHaveBeenCalledWith(false);
  });

  it("does not clear the submitting indicator for a superseded submission while a newer one is still in flight, but does clear it once the newer one finishes", async () => {
    const older = deferred<ImportJob>();
    const newer = deferred<ImportJob>();
    const submissionGuard = createSubmissionGuard();
    const contentGuard = createSubmissionGuard();
    const setSubmitting = vi.fn();

    const olderRequestId = submissionGuard.bump();
    const olderRun = submitValidationBatch(
      { businessId: "business-A", source: { fileName: "a.csv", format: "csv" }, profile, mapping: {}, text: "x", records: [] },
      {
        validateImport: () => older.promise,
        invalidateImportsCache: vi.fn(), markVerified: vi.fn(), markVerificationError: vi.fn(),
        setJob: vi.fn(), setSubmitError: vi.fn(), setSubmitting,
        isMounted: () => true,
        contentGuard, contentRequestId: contentGuard.bump(), submissionGuard, submissionRequestId: olderRequestId,
      },
    );

    // A second submission starts before the first resolves.
    const newerRequestId = submissionGuard.bump();
    const newerRun = submitValidationBatch(
      { businessId: "business-A", source: { fileName: "a.csv", format: "csv" }, profile, mapping: {}, text: "x", records: [] },
      {
        validateImport: () => newer.promise,
        invalidateImportsCache: vi.fn(), markVerified: vi.fn(), markVerificationError: vi.fn(),
        setJob: vi.fn(), setSubmitError: vi.fn(), setSubmitting,
        isMounted: () => true,
        contentGuard, contentRequestId: contentGuard.bump(), submissionGuard, submissionRequestId: newerRequestId,
      },
    );

    older.resolve(fakeJob("business-A"));
    await olderRun;
    // The older submission's own finally ran, but a newer one is active --
    // the indicator must still say "submitting".
    expect(setSubmitting).not.toHaveBeenCalled();

    newer.resolve(fakeJob("business-A"));
    await newerRun;
    expect(setSubmitting).toHaveBeenCalledWith(false);
  });

  it("never writes any React state after unmount on a successful response, but still invalidates the server-side cache (P2)", async () => {
    const { promise, resolve } = deferred<ImportJob>();
    const contentGuard = createSubmissionGuard();
    const submissionGuard = createSubmissionGuard();
    const setJob = vi.fn();
    const setSubmitting = vi.fn();
    const markVerified = vi.fn();
    const invalidateImportsCache = vi.fn();

    const contentRequestId = contentGuard.bump();
    const submissionRequestId = submissionGuard.bump();
    const run = submitValidationBatch(
      { businessId: "business-A", source: { fileName: "a.csv", format: "csv" }, profile, mapping: {}, text: "x", records: [] },
      {
        validateImport: () => promise,
        invalidateImportsCache, markVerified, markVerificationError: vi.fn(),
        setJob, setSubmitError: vi.fn(), setSubmitting,
        isMounted: () => false,
        contentGuard, contentRequestId, submissionGuard, submissionRequestId,
      },
    );

    resolve(fakeJob("business-A"));
    await run;

    // invalidateImportsCache does not write React state, so it is safe --
    // and expected -- to still run after unmount.
    expect(invalidateImportsCache).toHaveBeenCalledWith("business-A");
    expect(markVerified).not.toHaveBeenCalled();
    expect(setJob).not.toHaveBeenCalled();
    expect(setSubmitting).not.toHaveBeenCalled();
  });

  it("never writes any React state after unmount when the request fails (P2)", async () => {
    const { promise, reject } = deferred<ImportJob>();
    const contentGuard = createSubmissionGuard();
    const submissionGuard = createSubmissionGuard();
    const markVerificationError = vi.fn();
    const setSubmitError = vi.fn();
    const setSubmitting = vi.fn();

    const contentRequestId = contentGuard.bump();
    const submissionRequestId = submissionGuard.bump();
    const run = submitValidationBatch(
      { businessId: "business-A", source: { fileName: "a.csv", format: "csv" }, profile, mapping: {}, text: "x", records: [] },
      {
        validateImport: () => promise,
        invalidateImportsCache: vi.fn(), markVerified: vi.fn(), markVerificationError,
        setJob: vi.fn(), setSubmitError, setSubmitting,
        isMounted: () => false,
        contentGuard, contentRequestId, submissionGuard, submissionRequestId,
      },
    );

    reject(new Error("boom"));
    await run;

    expect(markVerificationError).not.toHaveBeenCalled();
    expect(setSubmitError).not.toHaveBeenCalled();
    expect(setSubmitting).not.toHaveBeenCalled();
  });

  it("marks the request's businessId as verified even when the response is stale (content changed mid-flight), as long as the component is still mounted -- but never shows the job under the wrong context", async () => {
    const { promise, resolve } = deferred<ImportJob>();
    const contentGuard = createSubmissionGuard();
    const submissionGuard = createSubmissionGuard();
    const markVerified = vi.fn();
    const setJob = vi.fn();

    const contentRequestId = contentGuard.bump();
    const submissionRequestId = submissionGuard.bump();
    const run = submitValidationBatch(
      { businessId: "business-A", source: { fileName: "a.csv", format: "csv" }, profile, mapping: {}, text: "x", records: [] },
      {
        validateImport: () => promise,
        invalidateImportsCache: vi.fn(), markVerified, markVerificationError: vi.fn(),
        setJob, setSubmitError: vi.fn(), setSubmitting: vi.fn(),
        isMounted: () => true,
        contentGuard, contentRequestId, submissionGuard, submissionRequestId,
      },
    );

    // The user edits the mapping (or switches business) before the
    // response lands -- content is now stale, but the component is still
    // mounted the whole time.
    contentGuard.bump();
    resolve(fakeJob("business-A"));
    await run;

    expect(markVerified).toHaveBeenCalledWith("business-A");
    expect(setJob).not.toHaveBeenCalled();
  });

  it("marks the request's businessId as a verification error even when stale, as long as the component is still mounted, when the server rejects it", async () => {
    const { promise, reject } = deferred<ImportJob>();
    const contentGuard = createSubmissionGuard();
    const submissionGuard = createSubmissionGuard();
    const markVerificationError = vi.fn();
    const setSubmitError = vi.fn();

    const contentRequestId = contentGuard.bump();
    const submissionRequestId = submissionGuard.bump();
    const run = submitValidationBatch(
      { businessId: "business-A", source: { fileName: "a.csv", format: "csv" }, profile, mapping: {}, text: "x", records: [] },
      {
        validateImport: () => promise,
        invalidateImportsCache: vi.fn(), markVerified: vi.fn(), markVerificationError,
        setJob: vi.fn(), setSubmitError, setSubmitting: vi.fn(),
        isMounted: () => true,
        contentGuard, contentRequestId, submissionGuard, submissionRequestId,
      },
    );

    contentGuard.bump();
    reject(new Error("boom"));
    await run;

    expect(markVerificationError).toHaveBeenCalledWith("business-A");
    expect(setSubmitError).not.toHaveBeenCalled();
  });
});

describe("trackMount (P0: React StrictMode double-invoke)", () => {
  it("ends up mounted=true after React StrictMode's dev-only setup -> cleanup -> setup sequence", () => {
    const ref = { current: false };
    // Exactly what StrictMode does in development, once, on initial mount.
    const cleanup1 = trackMount(ref);
    cleanup1();
    trackMount(ref);
    expect(ref.current).toBe(true);
  });

  it("marks unmounted once its own cleanup runs", () => {
    const ref = { current: false };
    const cleanup = trackMount(ref);
    expect(ref.current).toBe(true);
    cleanup();
    expect(ref.current).toBe(false);
  });
});

describe("applyFileRead (Hallazgo 3, second independent review: File.text() race)", () => {
  it("keeps the later file selection's fileName+text when the earlier selection's read resolves after it", async () => {
    const guard = createSubmissionGuard();
    const a = deferred<string>();
    const b = deferred<string>();
    const setFileName = vi.fn();
    const setText = vi.fn();
    const setFileReadError = vi.fn();

    const requestA = guard.bump();
    const runA = applyFileRead({ name: "a.csv", readText: () => a.promise }, { isMounted: () => true, guard, requestId: requestA, setFileName, setText, setFileReadError });
    const requestB = guard.bump();
    const runB = applyFileRead({ name: "b.csv", readText: () => b.promise }, { isMounted: () => true, guard, requestId: requestB, setFileName, setText, setFileReadError });

    // B (selected after A) resolves first here -- the ordinary case.
    b.resolve("b-content");
    await runB;
    a.resolve("a-content");
    await runA;

    expect(setFileName).toHaveBeenCalledTimes(1);
    expect(setFileName).toHaveBeenCalledWith("b.csv");
    expect(setText).toHaveBeenCalledTimes(1);
    expect(setText).toHaveBeenCalledWith("b-content");
  });

  it("keeps the later file selection's fileName+text even when the earlier selection's read resolves first (both possible orders land on B, never A)", async () => {
    const guard = createSubmissionGuard();
    const a = deferred<string>();
    const b = deferred<string>();
    const setFileName = vi.fn();
    const setText = vi.fn();
    const setFileReadError = vi.fn();

    const requestA = guard.bump();
    const runA = applyFileRead({ name: "a.csv", readText: () => a.promise }, { isMounted: () => true, guard, requestId: requestA, setFileName, setText, setFileReadError });
    const requestB = guard.bump();
    const runB = applyFileRead({ name: "b.csv", readText: () => b.promise }, { isMounted: () => true, guard, requestId: requestB, setFileName, setText, setFileReadError });

    // A (the stale one) resolves first this time; B still resolves after.
    a.resolve("a-content");
    await runA;
    b.resolve("b-content");
    await runB;

    expect(setFileName).toHaveBeenCalledTimes(1);
    expect(setFileName).toHaveBeenCalledWith("b.csv");
    expect(setText).toHaveBeenCalledTimes(1);
    expect(setText).toHaveBeenCalledWith("b-content");
  });

  it("applies fileName and text together, never one without the other", async () => {
    const guard = createSubmissionGuard();
    const { promise, resolve } = deferred<string>();
    const setFileName = vi.fn();
    const setText = vi.fn();
    const requestId = guard.bump();
    const run = applyFileRead({ name: "only.csv", readText: () => promise }, { isMounted: () => true, guard, requestId, setFileName, setText, setFileReadError: vi.fn() });
    resolve("only-content");
    await run;
    expect(setFileName).toHaveBeenCalledWith("only.csv");
    expect(setText).toHaveBeenCalledWith("only-content");
  });

  it("never writes state after unmount", async () => {
    const guard = createSubmissionGuard();
    const { promise, resolve } = deferred<string>();
    const setFileName = vi.fn();
    const setText = vi.fn();
    const requestId = guard.bump();
    const run = applyFileRead({ name: "a.csv", readText: () => promise }, { isMounted: () => false, guard, requestId, setFileName, setText, setFileReadError: vi.fn() });
    resolve("content");
    await run;
    expect(setFileName).not.toHaveBeenCalled();
    expect(setText).not.toHaveBeenCalled();
  });

  it("reports a useful read error without applying any fileName/text", async () => {
    const guard = createSubmissionGuard();
    const { promise, reject } = deferred<string>();
    const setFileName = vi.fn();
    const setText = vi.fn();
    const setFileReadError = vi.fn();
    const requestId = guard.bump();
    const run = applyFileRead({ name: "a.csv", readText: () => promise }, { isMounted: () => true, guard, requestId, setFileName, setText, setFileReadError });
    reject(new Error("disk error"));
    await run;
    expect(setFileName).not.toHaveBeenCalled();
    expect(setText).not.toHaveBeenCalled();
    expect(setFileReadError).toHaveBeenCalledWith("disk error");
  });

  it("discards a stale read's error the same way it discards a stale read's success", async () => {
    const guard = createSubmissionGuard();
    const { promise, reject } = deferred<string>();
    const setFileReadError = vi.fn();
    const requestId = guard.bump();
    const run = applyFileRead({ name: "a.csv", readText: () => promise }, { isMounted: () => true, guard, requestId, setFileName: vi.fn(), setText: vi.fn(), setFileReadError });
    guard.bump(); // a second file was selected before the first's read failed
    reject(new Error("disk error"));
    await run;
    expect(setFileReadError).not.toHaveBeenCalled();
  });
});

describe("filterAuditEntriesForBusiness (Hallazgo 2)", () => {
  const baseResult: ImportResult = { rows: [], issues: [], summary: { inputRows: 0, validRows: 0, errorRows: 0, totalMinor: 0 } };
  const entries: LocalAuditEntry[] = [
    { id: "id-1", at: "1", businessId: "business-A", businessName: "A", file: "a.csv", result: baseResult },
    { id: "id-2", at: "2", businessId: "business-B", businessName: "B", file: "b.csv", result: baseResult },
    { id: "id-3", at: "3", businessId: "business-A", businessName: "A", file: "a2.csv", result: baseResult },
  ];

  it("only returns entries for the requested business", () => {
    expect(filterAuditEntriesForBusiness(entries, "business-A").map((entry) => entry.at)).toEqual(["1", "3"]);
    expect(filterAuditEntriesForBusiness(entries, "business-B").map((entry) => entry.at)).toEqual(["2"]);
  });

  it("does not mutate or discard the underlying history -- switching back still shows it", () => {
    filterAuditEntriesForBusiness(entries, "business-A");
    expect(entries).toHaveLength(3);
    expect(filterAuditEntriesForBusiness(entries, "business-B")).toHaveLength(1);
  });

  it("returns nothing for a business with no local entries, including an invalid zero-row batch that never had rows to infer a businessId from", () => {
    expect(filterAuditEntriesForBusiness(entries, "business-C")).toEqual([]);
  });
});

describe("businessVerificationBadge (Hallazgo 3)", () => {
  it("is neutral before any submission has been made for this business", () => {
    expect(businessVerificationBadge("business-A", { status: "unverified" })).toEqual({ tone: "neutral", label: "Pendiente de verificación" });
  });

  it("is success only when the verified businessId matches the one currently selected", () => {
    expect(businessVerificationBadge("business-A", { status: "verified", businessId: "business-A" }).tone).toBe("success");
  });

  it("falls back to neutral, never a stale success, once the business changes away from the verified one", () => {
    expect(businessVerificationBadge("business-B", { status: "verified", businessId: "business-A" }).tone).toBe("neutral");
  });

  it("is danger only when the server error belongs to the currently selected business", () => {
    expect(businessVerificationBadge("business-A", { status: "error", businessId: "business-A" }).tone).toBe("danger");
  });

  it("never shows a green badge for a business that previously errored, even after switching away and back to unverified state", () => {
    const badge = businessVerificationBadge("business-B", { status: "error", businessId: "business-A" });
    expect(badge.tone).not.toBe("success");
    expect(badge.tone).toBe("neutral");
  });
});

describe("appendAuditEntry (P3: per-business retention)", () => {
  const baseResult: ImportResult = { rows: [], issues: [], summary: { inputRows: 0, validRows: 0, errorRows: 0, totalMinor: 0 } };
  const entryFor = (businessId: string, at: string) => ({ at, businessId, businessName: businessId, file: "f.csv", result: baseResult });

  it("keeps up to 10 entries for business A and 10 for business B, independently", () => {
    let entries: LocalAuditEntry[] = [];
    for (let i = 0; i < 10; i += 1) entries = appendAuditEntry(entries, entryFor("business-A", `a-${i}`));
    for (let i = 0; i < 10; i += 1) entries = appendAuditEntry(entries, entryFor("business-B", `b-${i}`));

    expect(filterAuditEntriesForBusiness(entries, "business-A")).toHaveLength(10);
    expect(filterAuditEntriesForBusiness(entries, "business-B")).toHaveLength(10);
    expect(entries).toHaveLength(20);
  });

  it("an 11th entry for business A evicts only business A's oldest entry, leaving business B untouched", () => {
    let entries: LocalAuditEntry[] = [];
    for (let i = 0; i < 10; i += 1) entries = appendAuditEntry(entries, entryFor("business-A", `a-${i}`));
    for (let i = 0; i < 3; i += 1) entries = appendAuditEntry(entries, entryFor("business-B", `b-${i}`));

    entries = appendAuditEntry(entries, entryFor("business-A", "a-10-newest"));

    const forA = filterAuditEntriesForBusiness(entries, "business-A");
    expect(forA).toHaveLength(10);
    expect(forA.map((entry) => entry.at)).not.toContain("a-0");
    expect(forA.map((entry) => entry.at)).toContain("a-10-newest");
    // Business B never had an 11th entry pushed -- its own 3 are all still there.
    expect(filterAuditEntriesForBusiness(entries, "business-B")).toHaveLength(3);
  });

  it("the tab counter (via filterAuditEntriesForBusiness) reflects only the selected business after mixed appends", () => {
    let entries: LocalAuditEntry[] = [];
    for (let i = 0; i < 12; i += 1) entries = appendAuditEntry(entries, entryFor("business-A", `a-${i}`));
    entries = appendAuditEntry(entries, entryFor("business-B", "b-0"));

    expect(filterAuditEntriesForBusiness(entries, "business-A")).toHaveLength(10);
    expect(filterAuditEntriesForBusiness(entries, "business-B")).toHaveLength(1);
    expect(filterAuditEntriesForBusiness(entries, "business-C")).toHaveLength(0);
  });

  it("assigns each entry its own id, never colliding even when two entries share the exact same `at` timestamp", () => {
    let entries: LocalAuditEntry[] = [];
    entries = appendAuditEntry(entries, entryFor("business-A", "same-millisecond"));
    entries = appendAuditEntry(entries, entryFor("business-A", "same-millisecond"));

    expect(entries).toHaveLength(2);
    expect(entries[0].id).not.toBe(entries[1].id);
    expect(new Set(entries.map((entry) => entry.id)).size).toBe(2);
  });
});


// --- Alcance 1 (hardening): timeout / cancelación de la validación ETL.

function abortAwareValidate(pending: { promise: Promise<ImportJob> }) {
  // Behaves like the real transport: rejects with RequestCancelledError as
  // soon as the caller's signal aborts, otherwise follows `pending`.
  return (_businessId: string, _body: unknown, _key: string, options?: { signal?: AbortSignal }) => new Promise<ImportJob>((resolve, reject) => {
    // Like httpRequest: a signal that aborted before the call (the cancel
    // can land while the idempotency signature is still being computed)
    // rejects immediately instead of waiting for an event that already fired.
    if (options?.signal?.aborted) return reject(new RequestCancelledError());
    options?.signal?.addEventListener("abort", () => reject(new RequestCancelledError()));
    pending.promise.then(resolve, reject);
  });
}

function submitDeps(overrides: Partial<Parameters<typeof submitValidationBatch>[1]> = {}) {
  const contentGuard = createSubmissionGuard();
  const submissionGuard = createSubmissionGuard();
  const deps = {
    validateImport: vi.fn(),
    invalidateImportsCache: vi.fn(), markVerified: vi.fn(), markVerificationError: vi.fn(),
    setJob: vi.fn(), setSubmitError: vi.fn(), setSubmitting: vi.fn(),
    isMounted: () => true,
    contentGuard, contentRequestId: contentGuard.bump(),
    submissionGuard, submissionRequestId: submissionGuard.bump(),
    ...overrides,
  } as Parameters<typeof submitValidationBatch>[1];
  return deps;
}

const submitRequest = { businessId: "business-A", source: { fileName: "a.csv", format: "csv" as const }, profile, mapping: {}, text: "x", records: [] };

describe("createRequestScope: one in-flight validation, cancelled when its context goes away", () => {
  it("aborts the previous signal when a new request begins, and the newest stays live", () => {
    const scope = createRequestScope();
    const first = scope.begin();
    const second = scope.begin();
    expect(first.aborted).toBe(true);
    expect(second.aborted).toBe(false);
  });

  it("cancel() aborts whatever is in flight and is safe to call with nothing in flight", () => {
    const scope = createRequestScope();
    expect(() => scope.cancel()).not.toThrow();
    const signal = scope.begin();
    scope.cancel();
    expect(signal.aborted).toBe(true);
    expect(() => scope.cancel()).not.toThrow();
  });
});

describe("submitValidationBatch: timeout, cancellation and unmount never leave the UI stuck or lying", () => {
  beforeEach(() => {
    const store = new Map<string, string>();
    vi.stubGlobal("window", { sessionStorage: { getItem: (k: string) => store.get(k) ?? null, setItem: (k: string, v: string) => store.set(k, v), removeItem: (k: string) => store.delete(k) } });
  });
  afterEach(() => { vi.unstubAllGlobals(); });

  it("passes the caller's signal to validateImport", async () => {
    const scope = createRequestScope();
    const signal = scope.begin();
    const deps = submitDeps({ signal });
    (deps.validateImport as ReturnType<typeof vi.fn>).mockResolvedValue(fakeJob("business-A"));
    await submitValidationBatch(submitRequest, deps);
    expect((deps.validateImport as ReturnType<typeof vi.fn>).mock.calls[0][3]).toEqual({ signal });
  });

  it("an intentional cancellation is not shown as a business error, does not mark the business unverified, and releases the submitting indicator", async () => {
    const pending = deferred<ImportJob>();
    const scope = createRequestScope();
    const deps = submitDeps({ signal: scope.begin(), validateImport: abortAwareValidate(pending) });
    const run = submitValidationBatch(submitRequest, deps);
    scope.cancel(); // what changing business/file/mapping does
    await run;

    expect(deps.setSubmitError).not.toHaveBeenCalled();
    expect(deps.markVerificationError).not.toHaveBeenCalled();
    expect(deps.markVerified).not.toHaveBeenCalled();
    expect(deps.setJob).not.toHaveBeenCalled();
    expect(deps.setSubmitting).toHaveBeenCalledWith(false);
    // The request may have reached the server before it was cancelled, so
    // the original business's Auditoría list is refreshed regardless.
    expect(deps.invalidateImportsCache).toHaveBeenCalledWith("business-A");
  });

  it("switching business mid-flight cancels the request for the old business and shows nothing from it", async () => {
    const pending = deferred<ImportJob>();
    const scope = createRequestScope();
    const deps = submitDeps({ signal: scope.begin(), validateImport: abortAwareValidate(pending) });
    const run = submitValidationBatch(submitRequest, deps);
    deps.contentGuard.bump();
    scope.cancel();
    pending.resolve(fakeJob("business-A")); // a late answer for A must not appear
    await run;
    expect(deps.setJob).not.toHaveBeenCalled();
    expect(deps.setSubmitError).not.toHaveBeenCalled();
    expect(deps.setSubmitting).toHaveBeenCalledWith(false);
  });

  it("a timeout is reported as an error and releases the submitting indicator", async () => {
    const deps = submitDeps({ validateImport: vi.fn().mockRejectedValue(new ApiError(0, "request_timeout")) });
    await submitValidationBatch(submitRequest, deps);
    expect(deps.setSubmitError).toHaveBeenCalledWith(expect.objectContaining({ status: 0, code: "request_timeout" }));
    expect(deps.setJob).not.toHaveBeenCalled();
    expect(deps.setSubmitting).toHaveBeenCalledWith(false);
  });

  it("a network error and an HTTP 5xx are each reported as errors and release the indicator", async () => {
    for (const error of [new ApiError(0, "network_error"), new ApiError(503, "unavailable")]) {
      const deps = submitDeps({ validateImport: vi.fn().mockRejectedValue(error) });
      await submitValidationBatch(submitRequest, deps);
      expect(deps.setSubmitError).toHaveBeenCalledWith(error);
      expect(deps.setSubmitting).toHaveBeenCalledWith(false);
    }
  });

  it("after unmount, not one state setter runs -- not on success, error, timeout, or cancellation", async () => {
    const outcomes: Array<() => Promise<ImportJob>> = [
      () => Promise.resolve(fakeJob("business-A")),
      () => Promise.reject(new ApiError(0, "request_timeout")),
      () => Promise.reject(new ApiError(500, "boom")),
      () => Promise.reject(new RequestCancelledError()),
    ];
    for (const outcome of outcomes) {
      const deps = submitDeps({ isMounted: () => false, validateImport: vi.fn(outcome) });
      await submitValidationBatch(submitRequest, deps);
      expect(deps.setJob).not.toHaveBeenCalled();
      expect(deps.setSubmitError).not.toHaveBeenCalled();
      expect(deps.setSubmitting).not.toHaveBeenCalled();
      expect(deps.markVerified).not.toHaveBeenCalled();
      expect(deps.markVerificationError).not.toHaveBeenCalled();
    }
  });

  it("retrying the identical batch after a timeout reuses the SAME Idempotency-Key", async () => {
    const keys: string[] = [];
    const validateImport = vi.fn(async (_b: string, _body: unknown, key: string) => {
      keys.push(key);
      if (keys.length === 1) throw new ApiError(0, "request_timeout");
      return fakeJob("business-A");
    });
    await submitValidationBatch(submitRequest, submitDeps({ validateImport }));
    await submitValidationBatch(submitRequest, submitDeps({ validateImport }));
    expect(keys).toHaveLength(2);
    expect(keys[1]).toBe(keys[0]);
  });

  it("a different batch gets a different Idempotency-Key (a retry key is never reused across content)", async () => {
    const keys: string[] = [];
    const validateImport = vi.fn(async (_b: string, _body: unknown, key: string) => { keys.push(key); return fakeJob("business-A"); });
    await submitValidationBatch(submitRequest, submitDeps({ validateImport }));
    await submitValidationBatch({ ...submitRequest, text: "y" }, submitDeps({ validateImport }));
    expect(keys[1]).not.toBe(keys[0]);
  });
});
