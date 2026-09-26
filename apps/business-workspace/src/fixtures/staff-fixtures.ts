import type {
  BusinessBranding,
  Charge,
  ChargeCreation,
  ChargeDetail,
  Customer,
  MerchantConnection,
  ReviewItem,
  StaffApi,
  WorkItem,
} from "../api/contracts";

const createdAt = "2026-09-12T15:00:00Z";

let charges: Charge[] = [
  {
    chargeId: "fixture-charge-142",
    customerId: "fixture-customer-laura",
    customerName: "Laura Martínez",
    folio: "MT-000142",
    amountMinor: 125_000,
    allocatedMinor: 0,
    outstandingMinor: 125_000,
    currency: "MXN",
    description: "Anticipo de servicio",
    dueDate: "2026-09-20",
    cancelled: false,
    createdAt,
  },
  {
    chargeId: "fixture-charge-141",
    customerId: "fixture-customer-omar",
    customerName: "Omar López",
    folio: "MT-000141",
    amountMinor: 850_00,
    allocatedMinor: 850_00,
    outstandingMinor: 0,
    currency: "MXN",
    description: "Servicio de septiembre",
    dueDate: "2026-09-10",
    cancelled: false,
    createdAt: "2026-09-10T12:00:00Z",
  },
  {
    chargeId: "fixture-charge-140",
    customerId: "fixture-customer-isabel",
    customerName: "Isabel R.",
    folio: "MT-000140",
    amountMinor: 2_500_00,
    allocatedMinor: 0,
    outstandingMinor: 2_500_00,
    currency: "MXN",
    description: "Saldo de instalación",
    dueDate: "2026-09-05",
    cancelled: false,
    createdAt: "2026-09-05T12:00:00Z",
  },
];

let customers: Customer[] = [
  { customerId: "fixture-customer-laura", displayName: "Laura Martínez", email: "laura@example.test", outstandingMinor: 125_000, openChargeCount: 1 },
  { customerId: "fixture-customer-omar", displayName: "Omar López", email: null, outstandingMinor: 0, openChargeCount: 0 },
  { customerId: "fixture-customer-isabel", displayName: "Isabel R.", email: "isabel@example.test", outstandingMinor: 2_500_00, openChargeCount: 1 },
];

let reviews: ReviewItem[] = [
  {
    kind: "refund",
    id: "fixture-refund-401",
    paymentId: "fixture-payment-141",
    status: "review",
    amountMinor: 1_000,
    reason: "Mercado Pago respondió HTTP 401 al solicitar el reembolso.",
    updatedAt: "2026-09-11T18:00:00Z",
  },
  {
    kind: "payment",
    id: "fixture-payment-extra",
    providerPaymentId: "mp-example-extra",
    status: "approved",
    amountMinor: 300_00,
    currency: "MXN",
    reason: "extra_payment",
    updatedAt: "2026-09-10T16:45:00Z",
  },
];

function delay<T>(value: T): Promise<T> {
  return new Promise((resolve) => window.setTimeout(() => resolve(value), 120));
}

function findCustomer(customerId: string): Customer {
  const customer = customers.find((item) => item.customerId === customerId);
  if (!customer) throw new Error("Cliente no encontrado en datos de demostración.");
  return customer;
}

function detailFor(chargeId: string): ChargeDetail {
  const charge = charges.find((item) => item.chargeId === chargeId) ?? charges[0];
  const customer = findCustomer(charge.customerId);
  const settled = charge.outstandingMinor === 0;
  return {
    charge,
    customer,
    paymentLink: { state: "available_once", issuedAt: charge.createdAt },
    attempts: [{
      attemptId: "fixture-attempt-141",
      status: "ready",
      expectedAmountMinor: charge.amountMinor,
      createdAt: charge.createdAt,
      updatedAt: "2026-09-10T13:00:00Z",
    }],
    payments: settled ? [{
      paymentId: "fixture-payment-141",
      providerPaymentId: "mp-example-141",
      providerStatus: "approved",
      amountMinor: charge.amountMinor,
      currency: "MXN",
      environment: "test",
      providerLiveMode: true,
      observedAt: "2026-09-10T13:20:00Z",
      approvedAt: "2026-09-10T13:19:00Z",
      reviewReason: null,
    }] : [],
    allocations: settled ? [{
      allocationId: "fixture-allocation-141",
      paymentId: "fixture-payment-141",
      amountMinor: charge.amountMinor,
      createdAt: "2026-09-10T13:20:00Z",
    }] : [],
    adjustments: [],
    providerEvents: settled ? [{
      eventId: "fixture-event-141",
      eventType: "payment",
      processingStatus: "processed",
      receivedAt: "2026-09-10T13:20:00Z",
      reason: null,
    }] : [],
    refundOperations: settled ? [{
      refundId: "fixture-refund-401",
      paymentId: "fixture-payment-141",
      amountMinor: 1_000,
      status: "review",
      providerRefundId: null,
      updatedAt: "2026-09-11T18:00:00Z",
      lastError: "Mercado Pago respondió HTTP 401 al solicitar el reembolso.",
    }] : [],
  };
}

export const fixtureApi: StaffApi = {
  source: "fixture",
  listCustomers: async () => delay({ items: [...customers], hasMore: false }),
  listCharges: async () => delay([...charges]),
  getBusinessSummary: async () => {
    const open = charges.filter((charge) => !charge.cancelled && charge.outstandingMinor > 0);
    return delay({ openChargeCount: open.length, outstandingMinor: open.reduce((sum, charge) => sum + charge.outstandingMinor, 0) });
  },
  createCustomer: async (_businessId, input) => {
    const customer: Customer = {
      customerId: `fixture-customer-${crypto.randomUUID()}`,
      displayName: input.displayName,
      email: input.email?.trim() || null,
      outstandingMinor: 0,
      openChargeCount: 0,
    };
    customers = [customer, ...customers];
    return delay(customer);
  },
  createCharge: async (_businessId, input) => {
    const customer = findCustomer(input.customerId);
    const chargeId = `fixture-charge-${crypto.randomUUID()}`;
    const charge: Charge = {
      chargeId,
      customerId: input.customerId,
      customerName: customer.displayName,
      folio: `MT-${String(charges.length + 143).padStart(6, "0")}`,
      amountMinor: input.amountMinor,
      allocatedMinor: 0,
      outstandingMinor: input.amountMinor,
      currency: "MXN",
      description: input.description,
      dueDate: input.dueDate,
      cancelled: false,
      createdAt: new Date().toISOString(),
    };
    charges = [charge, ...charges];
    return delay<ChargeCreation>({
      chargeId,
      folio: charge.folio,
      paymentUrl: `https://pay.example.test/pay/${crypto.randomUUID()}`,
    });
  },
  cancelCharge: async (_businessId, chargeId) => {
    const charge = charges.find((item) => item.chargeId === chargeId);
    if (!charge) throw new Error("Cobro no encontrado.");
    charge.cancelled = true;
    return delay({ chargeId, folio: charge.folio, cancelled: true });
  },
  requestRefund: async (_businessId, paymentId) => delay({ refundId: `fixture-refund-${crypto.randomUUID()}`, amountMinor: 0, status: "requested", paymentId }),
  listReviews: async () => delay({ items: [...reviews], hasMore: false }),
  resolveReview: async (_businessId, item, input) => {
    if (input.action === "acknowledge" || item.kind !== "payment") {
      reviews = reviews.filter((review) => !(review.kind === item.kind && review.id === item.id));
    }
    return delay({ kind: item.kind, reviewId: item.id, action: input.action, outcome: input.action === "retry" ? "queued" : "acknowledged" });
  },
  getWorkQueue: async () => delay<WorkItem[]>([
    { kind: "overdue_charge", id: "fixture-charge-140", title: "MT-000140 · Isabel R.", detail: "Venció el 5 sep · Saldo pendiente", amountMinor: 2_500_00, dueDate: "2026-09-05" },
    { kind: "due_today", id: "fixture-charge-142", title: "MT-000142 · Laura Martínez", detail: "Vence hoy · Anticipo de servicio", amountMinor: 125_000, dueDate: "2026-09-12" },
    { kind: "review", id: "fixture-refund-401", title: "Reembolso bajo revisión", detail: "Proveedor bloqueó la solicitud; no hay devolución confirmada.", amountMinor: 1_000 },
  ]),
  getChargeDetail: async (_businessId, chargeId) => delay(detailFor(chargeId)),
  getMerchantConnection: async () => delay<MerchantConnection>({
    provider: "mercado_pago",
    state: "verified_test",
    environment: "test",
    credentialSource: "test_credentials",
    verifiedAt: "2026-09-11T14:10:00Z",
    displayAccount: "Vendedor de prueba verificado",
  }),
  getBranding: async () => delay<BusinessBranding>({ publicName: "Negocio de ejemplo", accent: "#ed684c", accentHover: "#d8563c", nav: "#101931", navAlt: "#172545", canvas: "#f5f5f2" }),
};
