import { describe, expect, it, vi } from "vitest";
import { clearOperation, getOrCreateOperation } from "./idempotency";

const values = new Map<string, string>();

vi.stubGlobal("window", {
  sessionStorage: {
    getItem: (key: string) => values.get(key) ?? null,
    setItem: (key: string, value: string) => values.set(key, value),
    removeItem: (key: string) => values.delete(key),
  },
});

describe("idempotency operation registry", () => {
  it("reuses a key after an interrupted retry", () => {
    const first = getOrCreateOperation("charge", "draft-1", () => ({ chargeKey: "staff-charge:one" }));
    const second = getOrCreateOperation("charge", "draft-1", () => ({ chargeKey: "staff-charge:two" }));
    expect(second.chargeKey).toBe(first.chargeKey);
  });

  it("clears only after an explicit terminal operation", () => {
    clearOperation("charge", "draft-1");
    const next = getOrCreateOperation("charge", "draft-1", () => ({ chargeKey: "staff-charge:next" }));
    expect(next.chargeKey).toBe("staff-charge:next");
  });
});
