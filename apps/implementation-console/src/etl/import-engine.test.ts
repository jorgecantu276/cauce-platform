import { describe, expect, it } from "vitest";
import { parseTabularText, suggestMapping, validateImport } from "./import-engine";

const profile = { name: "Piloto", delimiter: "auto", dateFormat: "dmy", decimalSeparator: "comma", currency: "MXN" } as const;
const text = [
  "Clave cliente\tCliente\tCorreo\tFolio\tMonto\tConcepto\tVencimiento",
  "CLI-1\tTaller Norte\tCOBROS@EXAMPLE.COM\tFAC-101\t1.250,50\tServicio mensual\t30/09/2026",
].join("\n");

describe("import engine", () => {
  it("detects pasted Excel tabs and suggests Spanish mappings", () => {
    const table = parseTabularText(text);
    expect(table.delimiter).toBe("tab");
    expect(suggestMapping(table.headers)).toEqual({
      customerExternalId: "Clave cliente", customerName: "Cliente", customerEmail: "Correo",
      chargeExternalId: "Folio", amount: "Monto", description: "Concepto", dueDate: "Vencimiento",
    });
  });

  it("normalizes a valid row to the canonical contract", () => {
    const table = parseTabularText(text);
    const result = validateImport({ businessId: "business-1", table, mapping: suggestMapping(table.headers), profile });
    expect(result.issues).toEqual([]);
    expect(result.summary).toEqual({ inputRows: 1, validRows: 1, errorRows: 0, totalMinor: 125050 });
    expect(result.rows[0]).toMatchObject({
      businessId: "business-1", sourceRow: 2,
      customer: { externalId: "CLI-1", displayName: "Taller Norte", email: "cobros@example.com" },
      charge: { externalId: "FAC-101", amountMinor: 125050, currency: "MXN", dueDate: "2026-09-30" },
    });
  });

  it("parses quoted CSV fields containing commas", () => {
    const table = parseTabularText('customer_id,customer_name,charge_id,amount,description,due_date\n1,"Empresa, SA",A-1,10.25,"Plan, septiembre",2026-09-30');
    const result = validateImport({ businessId: "b", table, mapping: suggestMapping(table.headers), profile: { ...profile, dateFormat: "iso", decimalSeparator: "dot" } });
    expect(result.rows[0].customer.displayName).toBe("Empresa, SA");
    expect(result.rows[0].charge.description).toBe("Plan, septiembre");
  });

  it("rejects invalid amounts, dates, emails, and duplicate charge references", () => {
    const table = parseTabularText([
      "customer_id,customer_name,email,charge_id,amount,description,due_date",
      "1,Uno,no-es-correo,A-1,0,Servicio,31/02/2026",
      "2,Dos,dos@example.com,A-1,12.999,Servicio,30/09/2026",
    ].join("\n"));
    const result = validateImport({ businessId: "b", table, mapping: suggestMapping(table.headers), profile });
    expect(result.rows).toEqual([]);
    expect(result.summary.errorRows).toBe(2);
    expect(result.issues.map((issue) => issue.field)).toEqual(expect.arrayContaining(["amount", "dueDate", "customerEmail", "chargeExternalId"]));
  });

  it("reports missing required mappings without producing importable rows", () => {
    const table = parseTabularText("Cliente\tMonto\nUno\t100");
    const result = validateImport({ businessId: "b", table, mapping: suggestMapping(table.headers), profile });
    expect(result.rows).toEqual([]);
    expect(result.issues.filter((issue) => issue.row === 1).length).toBeGreaterThan(0);
  });

  it("rejects duplicate and empty headers", () => {
    expect(() => parseTabularText("Cliente,cliente\nUno,Dos")).toThrow(/repetido/);
    expect(() => parseTabularText("Cliente,\nUno,Dos")).toThrow(/encabezado/);
  });
});
