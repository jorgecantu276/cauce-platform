import { ApiError, httpRequest, type RequestOptions } from "./http";
import { fixturePlatformApi } from "../fixtures/platform-fixtures";
import type { AuthAdapter } from "../auth/auth-adapter";
import type { ImportJob, ImportJobPage, ImportJobView, PlatformApi, PurgedImport, ValidateImportInput } from "./platform-contracts";

class HttpPlatformApi implements PlatformApi {
  readonly source = "live" as const;

  constructor(private readonly baseUrl: string, private readonly auth: Pick<AuthAdapter, "getAccessToken">) {}

  private request<T>(path: string, options: RequestInit & RequestOptions = {}): Promise<T> {
    return httpRequest<T>({ baseUrl: this.baseUrl, auth: this.auth }, path, options);
  }

  validateImport(businessId: string, input: ValidateImportInput, idempotencyKey: string, options?: RequestOptions): Promise<ImportJob> {
    return this.request(`/platform/businesses/${encodeURIComponent(businessId)}/imports/validate`, {
      ...options,
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify(input),
    });
  }

  async getImport(businessId: string, importId: string, options?: RequestOptions): Promise<ImportJobView | null> {
    try {
      return await this.request(`/platform/businesses/${encodeURIComponent(businessId)}/imports/${encodeURIComponent(importId)}`, options);
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) return null;
      throw error;
    }
  }

  applyImport(businessId: string, importId: string, idempotencyKey: string, options?: RequestOptions): Promise<ImportJobView> {
    // No body: the server takes everything from the path and its own stored
    // job. The client cannot influence what apply creates.
    return this.request(`/platform/businesses/${encodeURIComponent(businessId)}/imports/${encodeURIComponent(importId)}/apply`, {
      ...options,
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
    });
  }

  purgeImport(businessId: string, importId: string, idempotencyKey: string, options?: RequestOptions): Promise<PurgedImport> {
    return this.request(`/platform/businesses/${encodeURIComponent(businessId)}/imports/${encodeURIComponent(importId)}/purge`, {
      ...options,
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
    });
  }

  async listImports(businessId: string, options?: { limit?: number; cursor?: string | null } & RequestOptions): Promise<ImportJobPage> {
    const params = new URLSearchParams();
    if (options?.limit) params.set("limit", String(options.limit));
    if (options?.cursor) params.set("cursor", options.cursor);
    const query = params.toString();
    return this.request(`/platform/businesses/${encodeURIComponent(businessId)}/imports${query ? `?${query}` : ""}`, { signal: options?.signal, timeoutMs: options?.timeoutMs });
  }
}

class UnavailablePlatformApi implements PlatformApi {
  readonly source = "live" as const;
  private unavailable(): Promise<never> { return Promise.reject(new ApiError(0, "staff_api_not_configured")); }
  validateImport(): Promise<ImportJob> { return this.unavailable(); }
  getImport(): Promise<ImportJobView | null> { return this.unavailable(); }
  applyImport(): Promise<ImportJobView> { return this.unavailable(); }
  purgeImport(): Promise<PurgedImport> { return this.unavailable(); }
  listImports(): Promise<ImportJobPage> { return this.unavailable(); }
}

/** Reuses VITE_STAFF_API_BASE_URL -- platform routes live on the same API
 * Gateway HTTP API as tenant staff routes, just under a /platform prefix,
 * so there is no separate base URL to configure. */
export function createPlatformApi(auth: Pick<AuthAdapter, "getAccessToken">): PlatformApi {
  const baseUrl = import.meta.env.VITE_STAFF_API_BASE_URL?.replace(/\/$/, "");
  if (baseUrl) return new HttpPlatformApi(baseUrl, auth);
  return (import.meta.env.DEV || import.meta.env.VITE_ENABLE_FIXTURES === "true") ? fixturePlatformApi : new UnavailablePlatformApi();
}
