import { fixtureApi } from "../fixtures/staff-fixtures";
import type { AuthAdapter } from "../auth/auth-adapter";
import type { BusinessSummary, Charge, ChargeCreation, Customer, ListingPage, ReviewItem, ReviewResolution, StaffApi } from "./contracts";
import { ApiError, httpRequest, type RequestOptions } from "./http";

// Re-exported so every existing `import { ApiError } from "./client"` keeps
// working; the class itself lives with the shared transport in ./http.
export { ApiError };

class HttpStaffApi implements StaffApi {
  readonly source = "live" as const;

  constructor(private readonly baseUrl: string, private readonly auth: Pick<AuthAdapter, "getAccessToken">) {}

  private request<T>(path: string, options: RequestInit & RequestOptions = {}): Promise<T> {
    return httpRequest<T>({ baseUrl: this.baseUrl, auth: this.auth }, path, options);
  }

  async listCharges(businessId: string, options?: RequestOptions): Promise<Charge[]> {
    const result = await this.request<{ charges: Charge[] }>(`/businesses/${encodeURIComponent(businessId)}/charges`, options);
    return result.charges;
  }

  async listCustomers(businessId: string, options?: RequestOptions): Promise<ListingPage<Customer>> {
    const result = await this.request<{ customers: Customer[]; hasMore: boolean }>(`/businesses/${encodeURIComponent(businessId)}/customers`, options);
    return { items: result.customers, hasMore: result.hasMore };
  }

  getBusinessSummary(businessId: string, options?: RequestOptions): Promise<BusinessSummary> {
    return this.request(`/businesses/${encodeURIComponent(businessId)}/summary`, options);
  }

  getChargeDetail(businessId: string, chargeId: string, options?: RequestOptions): Promise<import("./contracts").ChargeDetail> {
    return this.request(`/businesses/${encodeURIComponent(businessId)}/charges/${encodeURIComponent(chargeId)}`, options);
  }

  createCustomer(businessId: string, input: { displayName: string; email?: string }, idempotencyKey: string): Promise<Customer> {
    return this.request(`/businesses/${encodeURIComponent(businessId)}/customers`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify(input),
    });
  }

  createCharge(
    businessId: string,
    input: { customerId: string; amountMinor: number; currency: "MXN"; description: string; dueDate: string },
    idempotencyKey: string,
  ): Promise<ChargeCreation> {
    return this.request(`/businesses/${encodeURIComponent(businessId)}/charges`, {
      method: "POST",
      headers: { "Idempotency-Key": idempotencyKey },
      body: JSON.stringify(input),
    });
  }

  cancelCharge(businessId: string, chargeId: string, idempotencyKey: string): Promise<{ chargeId: string; folio: string; cancelled: boolean }> {
    return this.request(`/businesses/${encodeURIComponent(businessId)}/charges/${encodeURIComponent(chargeId)}/cancel`, {
      method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: "{}",
    });
  }

  requestRefund(businessId: string, paymentId: string, input: { amountMinor?: number }, idempotencyKey: string): Promise<{ refundId: string; amountMinor: number; status: string }> {
    return this.request(`/businesses/${encodeURIComponent(businessId)}/payments/${encodeURIComponent(paymentId)}/refunds`, {
      method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify(input),
    });
  }

  async listReviews(businessId: string, options?: RequestOptions): Promise<ListingPage<ReviewItem>> {
    const result = await this.request<{ reviews: ReviewItem[]; hasMore: boolean }>(`/businesses/${encodeURIComponent(businessId)}/reviews`, options);
    return { items: result.reviews, hasMore: result.hasMore };
  }

  resolveReview(businessId: string, item: ReviewItem, input: { action: "retry" | "acknowledge"; note: string }, idempotencyKey: string): Promise<ReviewResolution> {
    return this.request(`/businesses/${encodeURIComponent(businessId)}/reviews/${encodeURIComponent(item.kind)}/${encodeURIComponent(item.id)}/resolve`, {
      method: "POST", headers: { "Idempotency-Key": idempotencyKey }, body: JSON.stringify(input),
    });
  }

  async getWorkQueue(businessId: string, options?: RequestOptions): Promise<import("./contracts").WorkItem[]> {
    const result = await this.request<{ items: import("./contracts").WorkItem[] }>(`/businesses/${encodeURIComponent(businessId)}/work-queue`, options);
    return result.items;
  }

  getMerchantConnection(businessId: string, options?: RequestOptions): Promise<import("./contracts").MerchantConnection> {
    return this.request(`/businesses/${encodeURIComponent(businessId)}/merchant-connection`, options);
  }

  getBranding(businessId: string, options?: RequestOptions): Promise<import("./contracts").BusinessBranding> {
    return this.request(`/businesses/${encodeURIComponent(businessId)}/branding`, options);
  }
}

class UnavailableStaffApi implements StaffApi {
  readonly source = "live" as const;
  private unavailable(): Promise<never> { return Promise.reject(new ApiError(0, "staff_api_not_configured")); }
  listCustomers(): Promise<ListingPage<Customer>> { return this.unavailable(); }
  listCharges(): Promise<Charge[]> { return this.unavailable(); }
  getBusinessSummary(): Promise<BusinessSummary> { return this.unavailable(); }
  getChargeDetail(): Promise<import("./contracts").ChargeDetail> { return this.unavailable(); }
  createCustomer(): Promise<Customer> { return this.unavailable(); }
  createCharge(): Promise<ChargeCreation> { return this.unavailable(); }
  cancelCharge(): Promise<{ chargeId: string; folio: string; cancelled: boolean }> { return this.unavailable(); }
  requestRefund(): Promise<{ refundId: string; amountMinor: number; status: string }> { return this.unavailable(); }
  listReviews(): Promise<ListingPage<ReviewItem>> { return this.unavailable(); }
  resolveReview(): Promise<ReviewResolution> { return this.unavailable(); }
  getWorkQueue(): Promise<import("./contracts").WorkItem[]> { return this.unavailable(); }
  getMerchantConnection(): Promise<import("./contracts").MerchantConnection> { return this.unavailable(); }
  getBranding(): Promise<import("./contracts").BusinessBranding> { return this.unavailable(); }
}

export function createStaffApi(auth: Pick<import("../auth/auth-adapter").AuthAdapter, "getAccessToken">): StaffApi {
  const baseUrl = import.meta.env.VITE_STAFF_API_BASE_URL?.replace(/\/$/, "");
  if (baseUrl) return new HttpStaffApi(baseUrl, auth);
  return (import.meta.env.DEV || import.meta.env.VITE_ENABLE_FIXTURES === "true") ? fixtureApi : new UnavailableStaffApi();
}
