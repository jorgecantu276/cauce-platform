import { describe, expect, it } from "vitest";
import { chargeState, formatMxn, parseMxnToMinor } from "./money";

describe("MXN minor-unit helpers", () => {
  it("formats integer centavos as MXN", () => {
    expect(formatMxn(125_050)).toContain("1,250.50");
  });

  it("parses decimal entry without float rounding", () => {
    expect(parseMxnToMinor("1,250.50")).toBe(125_050);
    expect(parseMxnToMinor("10.5")).toBe(1_050);
  });

  it("rejects negative, zero, and over-precision values", () => {
    expect(parseMxnToMinor("0")).toBeNull();
    expect(parseMxnToMinor("-10")).toBeNull();
    expect(parseMxnToMinor("1.999")).toBeNull();
  });

  it("labels cancellation, full payment, and partial payment deterministically", () => {
    expect(chargeState({ cancelled: true, amountMinor: 100, outstandingMinor: 100 }).label).toBe("Cancelado");
    expect(chargeState({ cancelled: false, amountMinor: 100, outstandingMinor: 0 }).label).toBe("Confirmado");
    expect(chargeState({ cancelled: false, amountMinor: 100, outstandingMinor: 25 }).label).toBe("Pago parcial");
    expect(chargeState({ cancelled: false, amountMinor: 100, outstandingMinor: 100 }).label).toBe("Pendiente");
  });
});
