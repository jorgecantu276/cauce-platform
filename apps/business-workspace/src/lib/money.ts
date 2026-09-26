const mxn = new Intl.NumberFormat("es-MX", {
  style: "currency",
  currency: "MXN",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

export function formatMxn(minor: number): string {
  if (!Number.isSafeInteger(minor)) return "Monto no disponible";
  return mxn.format(minor / 100);
}

/** Converts a Spanish-friendly decimal entry into integer centavos without floats. */
export function parseMxnToMinor(value: string): number | null {
  const compact = value.trim().replace(/\s/g, "");
  // MXN inputs commonly use 1,250.50. A lone comma with one or two digits is
  // also accepted as a decimal separator (for example, 10,5).
  const normalized = compact.includes(".")
    ? compact.replace(/,/g, "")
    : compact.replace(/,(\d{1,2})$/, ".$1").replace(/,/g, "");
  const match = /^(\d+)(?:\.(\d{1,2}))?$/.exec(normalized);
  if (!match) return null;
  const whole = Number(match[1]);
  const cents = Number((match[2] ?? "").padEnd(2, "0"));
  const minor = whole * 100 + cents;
  return Number.isSafeInteger(minor) && minor > 0 ? minor : null;
}

export function formatDate(iso: string): string {
  const date = new Date(`${iso.slice(0, 10)}T12:00:00`);
  return new Intl.DateTimeFormat("es-MX", { day: "numeric", month: "short", year: "numeric" }).format(date);
}

export function formatDateTime(iso: string): string {
  return new Intl.DateTimeFormat("es-MX", {
    day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit",
  }).format(new Date(iso));
}

export function chargeState(charge: { cancelled: boolean; amountMinor: number; outstandingMinor: number }): { tone: string; label: string } {
  if (charge.cancelled) return { tone: "neutral", label: "Cancelado" };
  if (charge.outstandingMinor === 0) return { tone: "success", label: "Confirmado" };
  if (charge.outstandingMinor < charge.amountMinor) return { tone: "warning", label: "Pago parcial" };
  return { tone: "warning", label: "Pendiente" };
}
