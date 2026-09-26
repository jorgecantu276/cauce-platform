import { describe, expect, it, vi, beforeEach } from "vitest";
import { ApiError } from "./http";
import { createPlatformApi } from "./platform-client";
import type { ValidateImportInput } from "./platform-contracts";

const auth = { getAccessToken: async () => "test-token" };

function jsonResponse(status: number, body: unknown): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => body,
  } as Response;
}

const input: ValidateImportInput = {
  source: { fileName: "cartera.csv", format: "csv" },
  profile: { name: "p", delimiter: "comma", dateFormat: "iso", decimalSeparator: "dot", currency: "MXN" },
  records: [{ sourceRow: 2, customer: { externalId: "CLI-1", displayName: "Uno", email: null }, charge: { externalId: "FAC-1", amountMinor: 100, currency: "MXN", description: "x", dueDate: "2026-09-30" } }],
};

beforeEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  vi.stubEnv("VITE_STAFF_API_BASE_URL", "https://api.example.test");
});

describe("platform HTTP client: request adaptation", () => {
  it("sends the idempotency key header and adapts a successful validate response", async () => {
    const fetchMock = vi.fn(async (url: string, options: RequestInit) => {
      expect(url).toBe("https://api.example.test/platform/businesses/business-1/imports/validate");
      expect(options.method).toBe("POST");
      const headers = options.headers as Headers;
      expect(headers.get("Idempotency-Key")).toBe("import-key-1");
      expect(headers.get("Authorization")).toBe("Bearer test-token");
      expect(JSON.parse(String(options.body))).toEqual(input);
      return jsonResponse(200, { importId: "import-1", businessId: "business-1", status: "validated" });
    });
    vi.stubGlobal("fetch", fetchMock);

    const api = createPlatformApi(auth);
    const job = await api.validateImport("business-1", input, "import-key-1");
    expect(job).toEqual({ importId: "import-1", businessId: "business-1", status: "validated" });
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("builds the query string for listImports from limit and cursor", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      expect(url).toBe("https://api.example.test/platform/businesses/business-1/imports?limit=10&cursor=abc");
      return jsonResponse(200, { items: [], hasMore: false, cursor: null });
    });
    vi.stubGlobal("fetch", fetchMock);

    const api = createPlatformApi(auth);
    await api.listImports("business-1", { limit: 10, cursor: "abc" });
  });

  it("omits query parameters entirely when none are given", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      expect(url).toBe("https://api.example.test/platform/businesses/business-1/imports");
      return jsonResponse(200, { items: [], hasMore: false, cursor: null });
    });
    vi.stubGlobal("fetch", fetchMock);

    const api = createPlatformApi(auth);
    await api.listImports("business-1");
  });
});

describe("platform HTTP client: error classification", () => {
  it.each([401, 403, 409])("surfaces HTTP %i as an ApiError with that status", async (status) => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(status, { error: "some_code" })));
    const api = createPlatformApi(auth);
    await expect(api.validateImport("business-1", input, "import-key-1")).rejects.toMatchObject({ status, code: "some_code" });
  });

  it("classifies a network failure as ApiError(0, network_error)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("Failed to fetch"); }));
    const api = createPlatformApi(auth);
    await expect(api.validateImport("business-1", input, "import-key-1")).rejects.toMatchObject({ status: 0, code: "network_error" });
  });

  it("translates a 404 on getImport into null instead of throwing", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(404, { error: "not_found" })));
    const api = createPlatformApi(auth);
    await expect(api.getImport("business-1", "missing")).resolves.toBeNull();
  });

  it("still throws a non-404 error from getImport", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(403, { error: "forbidden" })));
    const api = createPlatformApi(auth);
    await expect(api.getImport("business-1", "import-1")).rejects.toBeInstanceOf(ApiError);
  });
});

describe("platform HTTP client: apply and purge", () => {
  it("applyImport POSTs to /apply with the idempotency key and no body, and returns the server's job untouched", async () => {
    const serverJob = { importId: "import-1", businessId: "business-1", status: "applying", apply: { rowsTotal: 120, rowsProcessed: 50 } };
    const fetchMock = vi.fn(async (url: string, options: RequestInit) => {
      expect(url).toBe("https://api.example.test/platform/businesses/business-1/imports/import-1/apply");
      expect(options.method).toBe("POST");
      expect(options.body).toBeUndefined();
      const headers = options.headers as Headers;
      expect(headers.get("Idempotency-Key")).toBe("apply-key-1");
      expect(headers.get("Authorization")).toBe("Bearer test-token");
      return jsonResponse(200, serverJob);
    });
    vi.stubGlobal("fetch", fetchMock);
    await expect(createPlatformApi(auth).applyImport("business-1", "import-1", "apply-key-1")).resolves.toEqual(serverJob);
  });

  it("encodes path segments so an id can never alter the route", async () => {
    const fetchMock = vi.fn(async (url: string) => {
      expect(url).toBe("https://api.example.test/platform/businesses/b%2F1/imports/i%3F2/apply");
      return jsonResponse(200, {});
    });
    vi.stubGlobal("fetch", fetchMock);
    await createPlatformApi(auth).applyImport("b/1", "i?2", "apply-key-1");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it.each([[409, "import_not_applicable"], [409, "import_rows_unavailable"], [403, "forbidden"], [404, "not_found"]])("surfaces apply %i %s as an ApiError", async (status, code) => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(status, { error: code })));
    await expect(createPlatformApi(auth).applyImport("business-1", "import-1", "apply-key-1")).rejects.toMatchObject({ status, code });
  });

  it("purgeImport POSTs to /purge with the idempotency key", async () => {
    const fetchMock = vi.fn(async (url: string, options: RequestInit) => {
      expect(url).toBe("https://api.example.test/platform/businesses/business-1/imports/import-1/purge");
      expect(options.method).toBe("POST");
      expect((options.headers as Headers).get("Idempotency-Key")).toBe("purge-key-1");
      return jsonResponse(200, { importId: "import-1", status: "purged" });
    });
    vi.stubGlobal("fetch", fetchMock);
    await expect(createPlatformApi(auth).purgeImport("business-1", "import-1", "purge-key-1")).resolves.toMatchObject({ status: "purged" });
  });

  it("forwards an abort signal on apply", async () => {
    vi.stubGlobal("fetch", vi.fn((_url: string, init: RequestInit) => new Promise<Response>((_resolve, reject) => {
      init.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
    })));
    const controller = new AbortController();
    const pending = createPlatformApi(auth).applyImport("business-1", "import-1", "apply-key-1", { signal: controller.signal });
    controller.abort();
    await expect(pending).rejects.toMatchObject({ name: "RequestCancelledError" });
  });
});
