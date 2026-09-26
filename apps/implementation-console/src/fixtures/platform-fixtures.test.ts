import { beforeEach, describe, expect, it } from "vitest";
import { ApiError } from "../api/http";
import { fixturePlatformApi, resetFixturePlatformState } from "./platform-fixtures";
import type { ValidateImportInput } from "../api/platform-contracts";

const input: ValidateImportInput = {
  source: { fileName: "cartera.csv", format: "csv" },
  profile: { name: "p", delimiter: "comma", dateFormat: "iso", decimalSeparator: "dot", currency: "MXN" },
  records: [{ sourceRow: 2, customer: { externalId: "CLI-1", displayName: "Uno", email: null }, charge: { externalId: "FAC-1", amountMinor: 100, currency: "MXN", description: "x", dueDate: "2026-09-30" } }],
};

beforeEach(() => {
  resetFixturePlatformState();
});

describe("fixturePlatformApi business isolation (Hallazgo 7)", () => {
  it("gives two different jobs to the same idempotency key used in two different businesses", async () => {
    const jobA = await fixturePlatformApi.validateImport("business-A", input, "shared-key");
    const jobB = await fixturePlatformApi.validateImport("business-B", input, "shared-key");
    expect(jobA.importId).not.toBe(jobB.importId);
    expect(jobA.businessId).toBe("business-A");
    expect(jobB.businessId).toBe("business-B");
  });

  it("returns the same job for the same business, key, and payload", async () => {
    const first = await fixturePlatformApi.validateImport("business-A", input, "same-key");
    const second = await fixturePlatformApi.validateImport("business-A", input, "same-key");
    expect(first.importId).toBe(second.importId);
  });

  it("raises a 409 ApiError for the same key with a different payload in the same business", async () => {
    await fixturePlatformApi.validateImport("business-A", input, "conflict-key");
    const different: ValidateImportInput = { ...input, records: [{ ...input.records[0], charge: { ...input.records[0].charge, amountMinor: 999 } }] };
    await expect(fixturePlatformApi.validateImport("business-A", different, "conflict-key")).rejects.toMatchObject({ status: 409, code: "operation_conflict" });
    await expect(fixturePlatformApi.validateImport("business-A", different, "conflict-key")).rejects.toBeInstanceOf(ApiError);
  });

  it("getImport returns null for a job that belongs to a different business", async () => {
    const job = await fixturePlatformApi.validateImport("business-A", input, "lookup-key");
    await expect(fixturePlatformApi.getImport("business-B", job.importId)).resolves.toBeNull();
    await expect(fixturePlatformApi.getImport("business-A", job.importId)).resolves.toEqual(job);
  });

  it("listImports for one business never includes another business's jobs", async () => {
    const jobA = await fixturePlatformApi.validateImport("business-A", input, "list-key-a");
    await fixturePlatformApi.validateImport("business-B", input, "list-key-b");
    const pageA = await fixturePlatformApi.listImports("business-A");
    expect(pageA.items.map((job) => job.importId)).toEqual([jobA.importId]);
    expect(pageA.items.every((job) => job.businessId === "business-A")).toBe(true);
  });

  it("resetFixturePlatformState clears jobs between test cases", async () => {
    await fixturePlatformApi.validateImport("business-A", input, "reset-key");
    resetFixturePlatformState();
    const page = await fixturePlatformApi.listImports("business-A");
    expect(page.items).toEqual([]);
  });
});

describe("fixturePlatformApi payload equivalence matches the backend's canonical_digest (Hallazgo 4)", () => {
  const withCustomer = (patch: Partial<ValidateImportInput["records"][number]["customer"]>): ValidateImportInput => ({
    ...input,
    records: [{ ...input.records[0], customer: { ...input.records[0].customer, ...patch } }],
  });

  it("treats the same email in a different casing as the same payload", async () => {
    const first = await fixturePlatformApi.validateImport("business-A", withCustomer({ email: "cobros@example.test" }), "casing-key");
    const second = await fixturePlatformApi.validateImport("business-A", withCustomer({ email: "COBROS@EXAMPLE.TEST" }), "casing-key");
    expect(second.importId).toBe(first.importId);
  });

  it("treats incidental extra/collapsible whitespace as the same payload", async () => {
    const first = await fixturePlatformApi.validateImport("business-A", withCustomer({ displayName: "Ferretería del Norte" }), "whitespace-key");
    const second = await fixturePlatformApi.validateImport("business-A", withCustomer({ displayName: "  Ferretería   del   Norte  " }), "whitespace-key");
    expect(second.importId).toBe(first.importId);
  });

  it("still raises 409 for a real difference in amountMinor", async () => {
    await fixturePlatformApi.validateImport("business-A", input, "real-diff-amount");
    const differentAmount: ValidateImportInput = { ...input, records: [{ ...input.records[0], charge: { ...input.records[0].charge, amountMinor: input.records[0].charge.amountMinor + 1 } }] };
    await expect(fixturePlatformApi.validateImport("business-A", differentAmount, "real-diff-amount")).rejects.toMatchObject({ status: 409 });
  });

  it("still raises 409 for a real difference in source.fileName", async () => {
    await fixturePlatformApi.validateImport("business-A", input, "real-diff-source");
    const differentSource: ValidateImportInput = { ...input, source: { ...input.source, fileName: "otro-archivo.csv" } };
    await expect(fixturePlatformApi.validateImport("business-A", differentSource, "real-diff-source")).rejects.toMatchObject({ status: 409 });
  });

  it("still raises 409 for a real difference in profile.delimiter", async () => {
    await fixturePlatformApi.validateImport("business-A", input, "real-diff-profile");
    const differentProfile: ValidateImportInput = { ...input, profile: { ...input.profile, delimiter: "semicolon" } };
    await expect(fixturePlatformApi.validateImport("business-A", differentProfile, "real-diff-profile")).rejects.toMatchObject({ status: 409 });
  });

  it("ignores an unrecognized extra property, the same way the backend silently ignores it, instead of provoking an artificial conflict", async () => {
    const first = await fixturePlatformApi.validateImport("business-A", input, "extra-prop-key");
    const withExtra = { ...input, unexpectedTopLevelField: "should be ignored" } as unknown as ValidateImportInput;
    const second = await fixturePlatformApi.validateImport("business-A", withExtra, "extra-prop-key");
    expect(second.importId).toBe(first.importId);
  });

  it("still isolates by businessId even when payloads are equivalent after normalization", async () => {
    const jobA = await fixturePlatformApi.validateImport("business-A", input, "shared-key-normalized");
    const jobB = await fixturePlatformApi.validateImport("business-B", input, "shared-key-normalized");
    expect(jobA.importId).not.toBe(jobB.importId);
  });
});

function manyRecords(count: number): ValidateImportInput["records"] {
  return Array.from({ length: count }, (_, index) => ({
    sourceRow: index + 2,
    customer: { externalId: `CLI-${index}`, displayName: `Cliente ${index}`, email: null },
    charge: { externalId: `FAC-${index}`, amountMinor: 100 + index, currency: "MXN" as const, description: "x", dueDate: "2026-09-30" },
  }));
}

describe("fixturePlatformApi apply/purge mirror the server contract (dev mode only)", () => {
  it("a validated fixture job is never `applied` until an apply response says so", async () => {
    const job = await fixturePlatformApi.validateImport("business-A", input, "v-key");
    expect(job.status).toBe("validated");
    expect(job.apply).toBeNull();
  });

  it("applies a small batch in one call with server-shaped progress", async () => {
    const job = await fixturePlatformApi.validateImport("business-A", input, "v-key");
    const applied = await fixturePlatformApi.applyImport("business-A", job.importId, "a-key");
    expect(applied.status).toBe("applied");
    expect(applied.status === "applied" && applied.apply).toMatchObject({ rowsTotal: 1, rowsProcessed: 1, chargesCreated: 1, conflicts: 0, failed: 0 });
  });

  it("slices a big batch and reports honest in-between progress", async () => {
    const job = await fixturePlatformApi.validateImport("business-A", { ...input, records: manyRecords(120) }, "v-key-big");
    const first = await fixturePlatformApi.applyImport("business-A", job.importId, "a-key");
    expect(first.status).toBe("applying");
    expect(first.status !== "purged" && first.apply?.rowsProcessed).toBe(50);
    const second = await fixturePlatformApi.applyImport("business-A", job.importId, "a-key");
    const third = await fixturePlatformApi.applyImport("business-A", job.importId, "a-key");
    expect(second.status !== "purged" && second.apply?.rowsProcessed).toBe(100);
    expect(third.status).toBe("applied");
    expect((await fixturePlatformApi.getImport("business-A", job.importId))?.status).toBe("applied");
  });

  it("replays a finished apply for the same key and refuses another key (409 import_not_applicable)", async () => {
    const job = await fixturePlatformApi.validateImport("business-A", input, "v-key");
    const first = await fixturePlatformApi.applyImport("business-A", job.importId, "a-key");
    await expect(fixturePlatformApi.applyImport("business-A", job.importId, "a-key")).resolves.toEqual(first);
    await expect(fixturePlatformApi.applyImport("business-A", job.importId, "other-key")).rejects.toMatchObject({ status: 409, code: "import_not_applicable" });
  });

  it("never applies across businesses: another business gets a 404", async () => {
    const job = await fixturePlatformApi.validateImport("business-A", input, "v-key");
    await expect(fixturePlatformApi.applyImport("business-B", job.importId, "a-key")).rejects.toMatchObject({ status: 404 });
    expect((await fixturePlatformApi.getImport("business-A", job.importId))?.status).toBe("validated");
  });

  it("purge is idempotent, reads back as purged, and blocks a later apply", async () => {
    const job = await fixturePlatformApi.validateImport("business-A", input, "v-key");
    const purged = await fixturePlatformApi.purgeImport("business-A", job.importId, "p-key");
    expect(purged.status).toBe("purged");
    await expect(fixturePlatformApi.purgeImport("business-A", job.importId, "p-key-2")).resolves.toEqual(purged);
    expect((await fixturePlatformApi.getImport("business-A", job.importId))?.status).toBe("purged");
    await expect(fixturePlatformApi.applyImport("business-A", job.importId, "a-key")).rejects.toMatchObject({ status: 409, code: "import_not_applicable" });
  });
});
