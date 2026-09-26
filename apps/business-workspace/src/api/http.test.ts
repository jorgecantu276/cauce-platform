import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "./client";
import { createStaffApi } from "./client";
import { DEFAULT_REQUEST_TIMEOUT_MS, httpRequest, isRequestCancelled, RequestCancelledError } from "./http";

const auth = { getAccessToken: async () => "test-token" };
const config = { baseUrl: "https://api.example.test", auth };

function jsonResponse(status: number, body: unknown): Response {
  return { ok: status >= 200 && status < 300, status, json: async () => body } as Response;
}

/** A fetch that never settles on its own -- it only rejects the way a real
 * browser fetch does, with an AbortError, once its signal aborts. */
function hangingFetch() {
  return vi.fn((_url: string, init: RequestInit) => new Promise<Response>((_resolve, reject) => {
    init.signal?.addEventListener("abort", () => reject(new DOMException("The operation was aborted.", "AbortError")));
  }));
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  vi.stubEnv("VITE_STAFF_API_BASE_URL", "https://api.example.test");
});

afterEach(() => {
  vi.useRealTimers();
});

describe("httpRequest: timeout", () => {
  it("documents a bounded default timeout instead of waiting forever", () => {
    expect(DEFAULT_REQUEST_TIMEOUT_MS).toBeGreaterThan(0);
    expect(DEFAULT_REQUEST_TIMEOUT_MS).toBeLessThanOrEqual(60_000);
  });

  it("aborts a hung request after the default timeout and reports ApiError(0, request_timeout)", async () => {
    const fetchMock = hangingFetch();
    vi.stubGlobal("fetch", fetchMock);
    const pending = httpRequest(config, "/slow");
    const assertion = expect(pending).rejects.toMatchObject({ status: 0, code: "request_timeout" });
    await vi.advanceTimersByTimeAsync(DEFAULT_REQUEST_TIMEOUT_MS + 1);
    await assertion;
    expect((fetchMock.mock.calls[0][1] as RequestInit).signal?.aborted).toBe(true);
  });

  it("honours a per-request timeout override", async () => {
    vi.stubGlobal("fetch", hangingFetch());
    const pending = httpRequest(config, "/slow", { timeoutMs: 200 });
    const assertion = expect(pending).rejects.toMatchObject({ code: "request_timeout" });
    await vi.advanceTimersByTimeAsync(201);
    await assertion;
  });

  it("does not time out a request that answers in time, and leaves no timer behind", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, { ok: true })));
    await expect(httpRequest(config, "/fast")).resolves.toEqual({ ok: true });
    expect(vi.getTimerCount()).toBe(0);
  });

  it("times out while the access token is still being obtained, not only during fetch", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const stuckAuth = { getAccessToken: () => new Promise<string | null>(() => {}) };
    const pending = httpRequest({ baseUrl: config.baseUrl, auth: stuckAuth }, "/slow", { timeoutMs: 300 });
    const assertion = expect(pending).rejects.toMatchObject({ status: 0, code: "request_timeout" });
    await vi.advanceTimersByTimeAsync(301);
    await assertion;
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("times out while the response body is still streaming, instead of resolving an empty object", async () => {
    vi.stubGlobal("fetch", vi.fn((_url: string, init: RequestInit) => Promise.resolve({
      ok: true, status: 200,
      json: () => new Promise((_resolve, reject) => {
        init.signal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
      }),
    } as unknown as Response)));
    const pending = httpRequest(config, "/slow-body", { timeoutMs: 300 });
    const assertion = expect(pending).rejects.toMatchObject({ code: "request_timeout" });
    await vi.advanceTimersByTimeAsync(301);
    await assertion;
  });
});

describe("httpRequest: intentional cancellation", () => {
  it("rejects with RequestCancelledError (never an ApiError) when the caller aborts mid-flight", async () => {
    vi.stubGlobal("fetch", hangingFetch());
    const controller = new AbortController();
    const pending = httpRequest(config, "/slow", { signal: controller.signal });
    const assertion = expect(pending).rejects.toBeInstanceOf(RequestCancelledError);
    await vi.advanceTimersByTimeAsync(10);
    controller.abort();
    await assertion;
    await expect(pending).rejects.not.toBeInstanceOf(ApiError);
    expect(vi.getTimerCount()).toBe(0);
  });

  it("does not even call fetch when the signal is already aborted", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();
    controller.abort();
    await expect(httpRequest(config, "/x", { signal: controller.signal })).rejects.toBeInstanceOf(RequestCancelledError);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("is distinguishable from timeout, network and HTTP errors through isRequestCancelled", async () => {
    vi.stubGlobal("fetch", hangingFetch());
    const controller = new AbortController();
    const pending = httpRequest(config, "/slow", { signal: controller.signal });
    controller.abort();
    const error = await pending.catch((caught: unknown) => caught);
    expect(isRequestCancelled(error)).toBe(true);
    expect(isRequestCancelled(new ApiError(0, "request_timeout"))).toBe(false);
    expect(isRequestCancelled(new ApiError(0, "network_error"))).toBe(false);
    expect(isRequestCancelled(new ApiError(500, "boom"))).toBe(false);
  });

  it("cancels while the access token is still pending", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    const stuckAuth = { getAccessToken: () => new Promise<string | null>(() => {}) };
    const controller = new AbortController();
    const pending = httpRequest({ baseUrl: config.baseUrl, auth: stuckAuth }, "/x", { signal: controller.signal });
    controller.abort();
    await expect(pending).rejects.toBeInstanceOf(RequestCancelledError);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("forwards the caller's abort to the underlying fetch signal (external signals are preserved)", async () => {
    const fetchMock = hangingFetch();
    vi.stubGlobal("fetch", fetchMock);
    const controller = new AbortController();
    const pending = httpRequest(config, "/slow", { signal: controller.signal }).catch(() => undefined);
    await vi.advanceTimersByTimeAsync(1);
    const passed = (fetchMock.mock.calls[0][1] as RequestInit).signal as AbortSignal;
    expect(passed.aborted).toBe(false);
    controller.abort();
    expect(passed.aborted).toBe(true);
    await pending;
  });
});

describe("httpRequest: network and HTTP errors stay distinct", () => {
  it("classifies a rejected fetch as ApiError(0, network_error)", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new TypeError("Failed to fetch"); }));
    await expect(httpRequest(config, "/x")).rejects.toMatchObject({ status: 0, code: "network_error" });
  });

  it.each([400, 401, 403, 404, 409, 422, 500, 502, 503])("surfaces HTTP %i with the server's error code", async (status) => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(status, { error: "server_code" })));
    await expect(httpRequest(config, "/x")).rejects.toMatchObject({ status, code: "server_code" });
  });

  it("falls back to request_failed when an error body is not JSON", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => ({ ok: false, status: 502, json: async () => { throw new SyntaxError("Unexpected token <"); } }) as unknown as Response));
    await expect(httpRequest(config, "/x")).rejects.toMatchObject({ status: 502, code: "request_failed" });
  });
});

describe("HttpStaffApi uses the bounded transport", () => {
  it("HttpStaffApi times out a hung read", async () => {
    vi.stubGlobal("fetch", hangingFetch());
    const api = createStaffApi(auth);
    const pending = api.listCharges("business-1");
    const assertion = expect(pending).rejects.toMatchObject({ status: 0, code: "request_timeout" });
    await vi.advanceTimersByTimeAsync(DEFAULT_REQUEST_TIMEOUT_MS + 1);
    await assertion;
  });

  it("keeps existing reads working: a normal staff read still adapts its response", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse(200, { charges: [{ chargeId: "c1" }] })));
    const api = createStaffApi(auth);
    await expect(api.listCharges("business-1")).resolves.toEqual([{ chargeId: "c1" }]);
  });
});
