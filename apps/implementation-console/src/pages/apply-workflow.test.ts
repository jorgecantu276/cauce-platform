import { describe, expect, it, vi } from "vitest";
import { ApiError, RequestCancelledError } from "../api/http";
import type { ApplyProgress, ImportJob, ImportJobView, PurgedImport } from "../api/platform-contracts";
import {
  applyErrorMessage, applyStatusPill, conflictReasonLabel, describeApply, isApplySuccess, purgeErrorMessage, runApplyLoop, summarizePreview,
  type ApplyLoopDeps,
} from "./apply-workflow";

function progress(overrides: Partial<ApplyProgress> = {}): ApplyProgress {
  return {
    rowsTotal: 3, rowsProcessed: 3, customersCreated: 2, customersReused: 1, chargesCreated: 3, chargesReused: 0, conflicts: 0,
    failed: 0, leaseActive: false, lastErrorCode: null, startedAt: "2026-09-19T12:00:00+00:00", completedAt: "2026-09-19T12:00:01+00:00",
    conflictSample: [], conflictsTruncated: false, ...overrides,
  };
}

function job(status: ImportJob["status"], apply: ApplyProgress | null, summary: Partial<ImportJob["summary"]> = {}): ImportJob {
  return {
    importId: "import-1", businessId: "business-1", status,
    source: { fileName: "cartera.csv", format: "csv" },
    profile: { name: "p", delimiter: "comma", dateFormat: "iso", decimalSeparator: "dot", currency: "MXN" },
    summary: { inputRows: 3, validRows: 3, errorRows: 0, totalMinor: 300, ...summary },
    issues: [], issuesTruncated: false, createdAt: "2026-09-19T12:00:00+00:00", validatedAt: "2026-09-19T12:00:00+00:00",
    createdBySubject: "subject-1", rowsExpireAt: "2026-10-19T12:00:00+00:00", apply,
  };
}

const purged: PurgedImport = {
  importId: "import-1", businessId: "business-1", status: "purged", purgedAt: "2026-09-20T12:00:00+00:00",
  purge: { priorStatus: "validated", chunksDeleted: 1, rowResultsDeleted: 0, applyGuardsDeleted: 0 },
};

// --- server evidence is the only thing that can say "applied" ---------------------------

describe("describeApply / isApplySuccess: success needs server evidence", () => {
  it("a validated job is ready to apply and is NEVER a success", () => {
    expect(describeApply(job("validated", null))).toEqual({ kind: "ready" });
    expect(isApplySuccess(job("validated", null))).toBe(false);
  });

  it("applied with matching server progress is the only success", () => {
    expect(describeApply(job("applied", progress())).kind).toBe("applied");
    expect(isApplySuccess(job("applied", progress()))).toBe(true);
  });

  it.each([
    ["no apply evidence at all", job("applied", null)],
    ["fewer rows processed than the job has", job("applied", progress({ rowsProcessed: 2 }))],
    ["the total disagrees with the validated summary", job("applied", progress({ rowsTotal: 2, rowsProcessed: 2, chargesCreated: 2 }))],
    ["a conflict is present", job("applied", progress({ conflicts: 1 }))],
    ["a failure is recorded", job("applied", progress({ failed: 1, lastErrorCode: "dynamodb_error" }))],
    ["charges created+reused do not add up to the rows", job("applied", progress({ chargesCreated: 1, chargesReused: 1 }))],
  ])("does not claim success when status says applied but %s", (_label, candidate) => {
    expect(isApplySuccess(candidate)).toBe(false);
    expect(describeApply(candidate).kind).toBe("unverified");
  });

  it("review requires at least one conflict, otherwise it is unverified", () => {
    expect(describeApply(job("review", progress({ conflicts: 1, chargesCreated: 2, customersCreated: 1 }))).kind).toBe("review");
    expect(describeApply(job("review", progress({ conflicts: 0 }))).kind).toBe("unverified");
    expect(isApplySuccess(job("review", progress({ conflicts: 1 })))).toBe(false);
  });

  it("applying is in progress while a lease is live and resumable otherwise", () => {
    expect(describeApply(job("applying", progress({ rowsProcessed: 1, leaseActive: true, completedAt: null }))).kind).toBe("in_progress");
    expect(describeApply(job("applying", progress({ rowsProcessed: 1, completedAt: null })))).toMatchObject({ kind: "resumable", failed: false });
    expect(describeApply(job("applying", progress({ rowsProcessed: 1, failed: 1, lastErrorCode: "dynamodb_error", completedAt: null }))))
      .toMatchObject({ kind: "resumable", failed: true, errorCode: "dynamodb_error" });
  });

  it("invalid, purging and purged jobs are never applicable", () => {
    expect(describeApply(job("invalid", null)).kind).toBe("invalid");
    expect(describeApply(job("purging", null)).kind).toBe("purging");
    expect(describeApply(purged).kind).toBe("purged");
    for (const candidate of [job("invalid", null), job("purging", null), purged] as ImportJobView[]) expect(isApplySuccess(candidate)).toBe(false);
  });
});

// --- the continuation loop -------------------------------------------------------------------

function loopDeps(responses: Array<ImportJobView | Error>, extra: Partial<ApplyLoopDeps> = {}) {
  const calls: Array<{ signal?: AbortSignal }> = [];
  const seen: ImportJobView[] = [];
  let index = 0;
  const deps: ApplyLoopDeps = {
    applyImport: vi.fn(async (options) => {
      calls.push(options);
      const next = responses[Math.min(index, responses.length - 1)];
      index += 1;
      if (next instanceof Error) throw next;
      return next;
    }),
    onProgress: (value) => { seen.push(value); },
    isActive: () => true,
    wait: async () => {},
    ...extra,
  };
  return { deps, calls, seen };
}

describe("runApplyLoop", () => {
  it("keeps calling until the server reports a final state and returns exactly what the server said", async () => {
    const slice1 = job("applying", progress({ rowsProcessed: 1, completedAt: null }));
    const slice2 = job("applying", progress({ rowsProcessed: 2, completedAt: null }));
    const done = job("applied", progress());
    const { deps, seen } = loopDeps([slice1, slice2, done]);
    const outcome = await runApplyLoop(deps);
    expect(outcome).toEqual({ kind: "finished", job: done });
    expect(seen).toEqual([slice1, slice2, done]);
    expect(deps.applyImport).toHaveBeenCalledTimes(3);
  });

  it("an interrupted timeout is uncertain, never a success, and is retryable", async () => {
    const { deps, seen } = loopDeps([new ApiError(0, "request_timeout")]);
    const outcome = await runApplyLoop(deps);
    expect(outcome).toMatchObject({ kind: "uncertain" });
    expect((outcome as { error: ApiError }).error.code).toBe("request_timeout");
    expect(seen).toEqual([]);  // nothing was reported as progress or success
  });

  it("a network error is uncertain as well", async () => {
    expect(await runApplyLoop(loopDeps([new ApiError(0, "network_error")]).deps)).toMatchObject({ kind: "uncertain" });
  });

  it("an HTTP answer (409/403/5xx) is a refusal, not an uncertainty", async () => {
    for (const status of [409, 403, 500]) {
      expect(await runApplyLoop(loopDeps([new ApiError(status, "some_code")]).deps)).toMatchObject({ kind: "refused" });
    }
  });

  it("an intentional cancellation is silent: no progress, no error, and it stops calling", async () => {
    const { deps, seen } = loopDeps([new RequestCancelledError()]);
    expect(await runApplyLoop(deps)).toEqual({ kind: "cancelled" });
    expect(seen).toEqual([]);
    expect(deps.applyImport).toHaveBeenCalledTimes(1);
  });

  it("stops without touching state once the context is no longer active (business changed / unmounted)", async () => {
    let active = true;
    const { deps, seen } = loopDeps([job("applying", progress({ rowsProcessed: 1, completedAt: null })), job("applied", progress())], {
      isActive: () => active,
      onProgress: () => { active = false; },
    });
    expect(await runApplyLoop(deps)).toEqual({ kind: "cancelled" });
    expect(seen).toEqual([]);
    expect(deps.applyImport).toHaveBeenCalledTimes(1);
  });

  it("does not report a response that arrives after the context went away", async () => {
    let active = true;
    const onProgress = vi.fn();
    const outcome = await runApplyLoop({
      applyImport: async () => { active = false; return job("applied", progress()); },
      onProgress, isActive: () => active, wait: async () => {},
    });
    expect(outcome).toEqual({ kind: "cancelled" });
    expect(onProgress).not.toHaveBeenCalled();
  });

  it("pauses on a server-reported recoverable failure and does not spin", async () => {
    const failed = job("applying", progress({ rowsProcessed: 2, failed: 1, lastErrorCode: "dynamodb_error", completedAt: null }));
    const { deps } = loopDeps([failed]);
    expect(await runApplyLoop(deps)).toEqual({ kind: "paused", job: failed });
    expect(deps.applyImport).toHaveBeenCalledTimes(1);
  });

  it("waits and polls while another invocation holds the lease", async () => {
    const wait = vi.fn(async () => {});
    const busy = job("applying", progress({ rowsProcessed: 1, leaseActive: true, completedAt: null }));
    const done = job("applied", progress());
    const { deps } = loopDeps([busy, busy, done], { wait, pollMs: 250 });
    expect(await runApplyLoop(deps)).toMatchObject({ kind: "finished" });
    expect(wait).toHaveBeenCalledTimes(2);
    expect(wait).toHaveBeenCalledWith(250, undefined);
  });

  it("stops as stalled when the server keeps answering without advancing (no unbounded loop)", async () => {
    const stuck = job("applying", progress({ rowsProcessed: 1, completedAt: null }));
    const { deps } = loopDeps([stuck]);
    const outcome = await runApplyLoop({ ...deps, maxCalls: 50 });
    expect(outcome).toMatchObject({ kind: "stalled" });
    expect((deps.applyImport as ReturnType<typeof vi.fn>).mock.calls.length).toBeLessThan(10);
  });

  it("is bounded by maxCalls even when it keeps advancing", async () => {
    let processed = 0;
    const deps: ApplyLoopDeps = {
      applyImport: async () => job("applying", progress({ rowsTotal: 1000, rowsProcessed: (processed += 1), completedAt: null })),
      onProgress: () => {}, isActive: () => true, wait: async () => {}, maxCalls: 5,
    };
    expect(await runApplyLoop(deps)).toMatchObject({ kind: "exhausted" });
    expect(processed).toBe(5);
  });

  it("passes the same signal on every call so cancelling aborts the one in flight", async () => {
    const controller = new AbortController();
    const { deps, calls } = loopDeps([job("applying", progress({ rowsProcessed: 1, completedAt: null })), job("applied", progress())], { signal: controller.signal });
    await runApplyLoop(deps);
    expect(calls.map((call) => call.signal)).toEqual([controller.signal, controller.signal]);
  });

  it("reports a purged answer as final (the server, not the client, decided)", async () => {
    expect(await runApplyLoop(loopDeps([purged]).deps)).toEqual({ kind: "finished", job: purged });
  });
});

// --- presentation helpers ------------------------------------------------------------------------

describe("summarizePreview", () => {
  it("counts rows, distinct customers and the total in integer minor units", () => {
    const rows = [
      { customer: { externalId: "A" }, charge: { amountMinor: 100050 } },
      { customer: { externalId: "A" }, charge: { amountMinor: 250 } },
      { customer: { externalId: "B" }, charge: { amountMinor: 9 } },
    ];
    expect(summarizePreview(rows)).toEqual({ rows: 3, customers: 2, totalMinor: 100309 });
    expect(Number.isInteger(summarizePreview(rows).totalMinor)).toBe(true);
  });

  it("handles an empty batch", () => {
    expect(summarizePreview([])).toEqual({ rows: 0, customers: 0, totalMinor: 0 });
  });
});

describe("applyStatusPill: validated is never labelled applied", () => {
  it("labels each status distinctly and honestly", () => {
    expect(applyStatusPill("validated")).toMatchObject({ tone: "neutral" });
    expect(applyStatusPill("validated").label).toMatch(/sin aplicar/i);
    expect(applyStatusPill("applied")).toMatchObject({ tone: "success" });
    expect(applyStatusPill("review").tone).toBe("warning");
    expect(applyStatusPill("invalid").tone).toBe("danger");
    expect(applyStatusPill("purged").tone).toBe("neutral");
    const labels = (["validated", "invalid", "applying", "applied", "review", "purging", "purged"] as const).map((status) => applyStatusPill(status).label);
    expect(new Set(labels).size).toBe(labels.length);
    expect(applyStatusPill("validated").label).not.toMatch(/^aplicad/i);
  });
});

describe("messages", () => {
  it("conflict reasons are explained in plain Spanish and never echo data", () => {
    for (const code of ["customer_identity_conflict", "charge_payload_conflict", "charge_cancelled", "customer_missing"]) {
      expect(conflictReasonLabel(code).length).toBeGreaterThan(10);
    }
    expect(conflictReasonLabel("something_new")).toMatch(/revisa/i);
  });

  it("an uncertain outcome never reads like success and promises no duplication", () => {
    const message = applyErrorMessage({ kind: "uncertain", error: new ApiError(0, "request_timeout") });
    expect(message).toMatch(/misma clave/i);
    expect(message).not.toMatch(/se aplic[óo] correctamente|aplicado/i);
    expect(applyErrorMessage({ kind: "uncertain", error: new ApiError(0, "network_error") })).toMatch(/misma clave/i);
  });

  it("refusals name the reason for known server codes", () => {
    expect(applyErrorMessage({ kind: "refused", error: new ApiError(409, "import_not_applicable") })).toMatch(/no se puede aplicar/i);
    expect(applyErrorMessage({ kind: "refused", error: new ApiError(409, "import_rows_unavailable") })).toMatch(/expir/i);
    expect(applyErrorMessage({ kind: "refused", error: new ApiError(409, "import_data_invalid") })).toMatch(/no coinciden|inválid/i);
    expect(applyErrorMessage({ kind: "refused", error: new ApiError(403, "forbidden") })).toMatch(/permiso/i);
    expect(applyErrorMessage({ kind: "refused", error: new ApiError(401, "x") })).toMatch(/sesión/i);
  });
});

describe("purgeErrorMessage", () => {
  it("explains a live apply, a permission problem, and an uncertain outcome without claiming the purge happened", () => {
    expect(purgeErrorMessage(new ApiError(409, "import_apply_active"))).toMatch(/en curso/i);
    expect(purgeErrorMessage(new ApiError(403, "forbidden"))).toMatch(/permiso/i);
    expect(purgeErrorMessage(new ApiError(401, "x"))).toMatch(/sesión/i);
    const uncertain = purgeErrorMessage(new ApiError(0, "request_timeout"));
    expect(uncertain).toMatch(/misma clave/i);
    expect(uncertain).not.toMatch(/se eliminaron correctamente/i);
    expect(purgeErrorMessage(new Error("boom"))).toMatch(/no se pudieron eliminar/i);
  });
});
