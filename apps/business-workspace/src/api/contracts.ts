import type { RequestOptions } from "./http";

export type StaffRole = "owner" | "staff";

export type StaffSession = {
  subject: string;
  displayName: string;
  /** Platform access is independent from membership in any tenant. The API is
   * the authority for this value; route visibility is only a UI convenience. */
  platformRole: "super_admin" | null;
  memberships: Array<{
    businessId: string;
    businessName: string;
    role: StaffRole;
    capabilities: string[];
  }>;
};

export type Customer = {
  customerId: string;
  displayName: string;
  email: string | null;
  /** Current non-cancelled amount due across this customer's charges. */
  outstandingMinor: number;
  openChargeCount: number;
};

export type Charge = {
  chargeId: string;
  customerId: string;
  /** Denormalized for the collections list; the API supplies a safe display fallback. */
  customerName: string;
  folio: string;
  amountMinor: number;
  allocatedMinor: number;
  outstandingMinor: number;
  currency: "MXN";
  description: string;
  dueDate: string;
  cancelled: boolean;
  createdAt: string;
};

export type ChargeCreation = {
  chargeId: string;
  folio: string;
  paymentUrl: string;
};

export type ReviewItem = {
  kind: "payment" | "refund" | "provider_event";
  id: string;
  paymentId?: string;
  providerPaymentId?: string;
  providerEventKey?: string;
  providerResourceId?: string;
  eventType?: string;
  status?: string;
  amountMinor?: number;
  currency?: "MXN";
  reason: string | null;
  updatedAt: string;
};

export type ReviewResolution = {
  kind: ReviewItem["kind"];
  reviewId: string;
  action: "retry" | "acknowledge";
  outcome: string;
};

export type PaymentEvidence = {
  paymentId: string;
  providerPaymentId: string;
  providerStatus: string;
  amountMinor: number;
  currency: "MXN";
  environment: "test" | "live";
  providerLiveMode: boolean | null;
  observedAt: string;
  approvedAt: string | null;
  reviewReason: string | null;
};

export type AllocationEvidence = {
  allocationId: string;
  paymentId: string;
  amountMinor: number;
  createdAt: string;
};

export type AdjustmentEvidence = {
  adjustmentId: string;
  paymentId: string;
  allocationId: string | null;
  kind: "refund" | "chargeback" | "reversal" | "correction";
  status: string;
  amountMinor: number;
  occurredAt: string | null;
};

export type AttemptEvidence = {
  attemptId: string;
  status: "creating" | "unknown" | "ready" | "failed" | "expiring" | "expired";
  expectedAmountMinor: number;
  createdAt: string;
  updatedAt: string;
};

export type ProviderEventEvidence = {
  eventId: string;
  eventType: string;
  processingStatus: "accepted" | "processing" | "processed" | "failed" | "review";
  receivedAt: string;
  reason: string | null;
};

export type RefundOperationEvidence = {
  refundId: string;
  paymentId: string;
  amountMinor: number;
  status: "requested" | "processing" | "unknown" | "accepted" | "completed" | "failed" | "review" | "resolved";
  providerRefundId: string | null;
  updatedAt: string;
  lastError: string | null;
};

export type ChargeDetail = {
  charge: Charge;
  customer: Customer;
  paymentLink: { state: "available_once" | "revoked" | "unavailable"; issuedAt: string | null };
  attempts: AttemptEvidence[];
  payments: PaymentEvidence[];
  allocations: AllocationEvidence[];
  adjustments: AdjustmentEvidence[];
  providerEvents: ProviderEventEvidence[];
  refundOperations: RefundOperationEvidence[];
};

export type BusinessSummary = {
  /** Aggregated over every open (non-cancelled, outstanding > 0) charge --
   * unlike listCharges, this total is never capped to a display page. */
  openChargeCount: number;
  outstandingMinor: number;
};

export type WorkItem = {
  kind: "overdue_charge" | "due_today" | "review";
  id: string;
  title: string;
  detail: string;
  amountMinor?: number;
  dueDate?: string;
};

export type MerchantConnection = {
  provider: "mercado_pago";
  state: "verified_test" | "unavailable" | "needs_configuration";
  environment: "test" | "live" | null;
  credentialSource: "test_credentials" | "production_credentials" | "oauth" | null;
  verifiedAt: string | null;
  displayAccount: string | null;
};

export type BusinessBranding = {
  publicName: string;
  accent: string;
  accentHover: string;
  nav: string;
  navAlt: string;
  canvas: string;
};

export type ListingPage<T> = {
  items: T[];
  hasMore: boolean;
};

export interface StaffApi {
  // Reads take an optional AbortSignal/timeout (see ./http.ts); mutations do
  // not: an uncertain mutation is retried with its own Idempotency-Key, never
  // silently cancelled by a screen change.
  readonly source: "live" | "fixture";
  listCustomers(businessId: string, options?: RequestOptions): Promise<ListingPage<Customer>>;
  listCharges(businessId: string, options?: RequestOptions): Promise<Charge[]>;
  getBusinessSummary(businessId: string, options?: RequestOptions): Promise<BusinessSummary>;
  getChargeDetail(businessId: string, chargeId: string, options?: RequestOptions): Promise<ChargeDetail>;
  createCustomer(businessId: string, input: { displayName: string; email?: string }, idempotencyKey: string): Promise<Customer>;
  createCharge(
    businessId: string,
    input: { customerId: string; amountMinor: number; currency: "MXN"; description: string; dueDate: string },
    idempotencyKey: string,
  ): Promise<ChargeCreation>;
  cancelCharge(businessId: string, chargeId: string, idempotencyKey: string): Promise<{ chargeId: string; folio: string; cancelled: boolean }>;
  requestRefund(businessId: string, paymentId: string, input: { amountMinor?: number }, idempotencyKey: string): Promise<{ refundId: string; amountMinor: number; status: string }>;
  listReviews(businessId: string, options?: RequestOptions): Promise<ListingPage<ReviewItem>>;
  resolveReview(businessId: string, item: ReviewItem, input: { action: "retry" | "acknowledge"; note: string }, idempotencyKey: string): Promise<ReviewResolution>;
  getWorkQueue(businessId: string, options?: RequestOptions): Promise<WorkItem[]>;
  getMerchantConnection(businessId: string, options?: RequestOptions): Promise<MerchantConnection>;
  getBranding(businessId: string, options?: RequestOptions): Promise<BusinessBranding>;
}
