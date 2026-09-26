export const canonicalFields = [
  "customerExternalId",
  "customerName",
  "customerEmail",
  "chargeExternalId",
  "amount",
  "description",
  "dueDate",
] as const;

export type CanonicalField = typeof canonicalFields[number];
export type DelimiterOption = "auto" | "tab" | "comma" | "semicolon";
export type DateFormat = "iso" | "dmy" | "mdy";
export type DecimalSeparator = "dot" | "comma";
export type HeaderMapping = Partial<Record<CanonicalField, string>>;

export type ImportProfile = {
  name: string;
  delimiter: DelimiterOption;
  dateFormat: DateFormat;
  decimalSeparator: DecimalSeparator;
  currency: "MXN";
};

export type ParsedTable = {
  headers: string[];
  rows: Array<Record<string, string>>;
  delimiter: Exclude<DelimiterOption, "auto">;
};

export type CanonicalImportRow = {
  businessId: string;
  sourceRow: number;
  customer: { externalId: string; displayName: string; email: string | null };
  charge: {
    externalId: string;
    amountMinor: number;
    currency: "MXN";
    description: string;
    dueDate: string;
  };
};

export type ImportIssue = {
  row: number;
  field: CanonicalField | "file";
  message: string;
};

export type ImportResult = {
  rows: CanonicalImportRow[];
  issues: ImportIssue[];
  summary: { inputRows: number; validRows: number; errorRows: number; totalMinor: number };
};

const aliases: Record<CanonicalField, string[]> = {
  customerExternalId: ["customer id", "customer_id", "cliente id", "id cliente", "clave cliente", "codigo cliente", "código cliente"],
  customerName: ["customer name", "customer_name", "cliente", "nombre cliente", "razon social", "razón social"],
  customerEmail: ["customer email", "customer_email", "email", "correo", "correo cliente"],
  chargeExternalId: ["charge id", "charge_id", "cobro id", "id cobro", "folio", "factura", "referencia"],
  amount: ["amount", "importe", "monto", "saldo", "total"],
  description: ["description", "descripcion", "descripción", "concepto"],
  dueDate: ["due date", "due_date", "fecha vencimiento", "vencimiento", "fecha limite", "fecha límite"],
};

function normalizedHeader(value: string): string {
  return value.trim().toLocaleLowerCase("es-MX").replace(/\s+/g, " ");
}

export function suggestMapping(headers: string[]): HeaderMapping {
  const available = new Map(headers.map((header) => [normalizedHeader(header), header]));
  return Object.fromEntries(canonicalFields.flatMap((field) => {
    const match = aliases[field].map(normalizedHeader).map((candidate) => available.get(candidate)).find(Boolean);
    return match ? [[field, match]] : [];
  })) as HeaderMapping;
}

function separatorFor(option: DelimiterOption, firstLine: string): { character: string; name: ParsedTable["delimiter"] } {
  if (option === "tab") return { character: "\t", name: "tab" };
  if (option === "comma") return { character: ",", name: "comma" };
  if (option === "semicolon") return { character: ";", name: "semicolon" };
  const candidates = [{ character: "\t", name: "tab" as const }, { character: ",", name: "comma" as const }, { character: ";", name: "semicolon" as const }];
  return candidates.map((candidate) => ({ ...candidate, count: fieldsFor(firstLine, candidate.character).length }))
    .sort((left, right) => right.count - left.count)[0];
}

function fieldsFor(line: string, separator: string): string[] {
  const fields: string[] = [];
  let value = "";
  let quoted = false;
  for (let index = 0; index < line.length; index += 1) {
    const character = line[index];
    if (character === '"') {
      if (quoted && line[index + 1] === '"') { value += '"'; index += 1; }
      else quoted = !quoted;
    } else if (character === separator && !quoted) {
      fields.push(value.trim()); value = "";
    } else value += character;
  }
  fields.push(value.trim());
  return fields;
}

export function parseTabularText(text: string, option: DelimiterOption = "auto"): ParsedTable {
  const lines = text.replace(/^\uFEFF/, "").split(/\r?\n/).filter((line) => line.trim().length > 0);
  if (lines.length === 0) return { headers: [], rows: [], delimiter: option === "auto" ? "tab" : option };
  const separator = separatorFor(option, lines[0]);
  const headers = fieldsFor(lines[0], separator.character);
  const seen = new Set<string>();
  for (const header of headers) {
    if (!header) throw new Error("Todas las columnas necesitan encabezado.");
    const key = normalizedHeader(header);
    if (seen.has(key)) throw new Error(`El encabezado \"${header}\" está repetido.`);
    seen.add(key);
  }
  const rows = lines.slice(1).map((line) => {
    const values = fieldsFor(line, separator.character);
    return Object.fromEntries(headers.map((header, index) => [header, values[index]?.trim() ?? ""]));
  });
  return { headers, rows, delimiter: separator.name };
}

function mappedValue(row: Record<string, string>, mapping: HeaderMapping, field: CanonicalField): string {
  const header = mapping[field];
  return header ? (row[header] ?? "").trim() : "";
}

function parseAmountMinor(value: string, decimalSeparator: DecimalSeparator): number | null {
  const compact = value.replace(/\s/g, "").replace(/^MXN\$?/i, "").replace(/^\$/, "");
  const normalized = decimalSeparator === "comma"
    ? compact.replace(/\./g, "").replace(",", ".")
    : compact.replace(/,/g, "");
  if (!/^\d+(?:\.\d{1,2})?$/.test(normalized)) return null;
  const minor = Math.round(Number(normalized) * 100);
  return Number.isSafeInteger(minor) && minor > 0 ? minor : null;
}

function normalizedDate(value: string, format: DateFormat): string | null {
  let year: number;
  let month: number;
  let day: number;
  if (format === "iso") {
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
    if (!match) return null;
    [, year, month, day] = match.map(Number);
  } else {
    const match = /^(\d{1,2})[\/-](\d{1,2})[\/-](\d{4})$/.exec(value);
    if (!match) return null;
    const first = Number(match[1]); const second = Number(match[2]); year = Number(match[3]);
    [day, month] = format === "dmy" ? [first, second] : [second, first];
  }
  const candidate = new Date(Date.UTC(year, month - 1, day));
  if (candidate.getUTCFullYear() !== year || candidate.getUTCMonth() !== month - 1 || candidate.getUTCDate() !== day) return null;
  return `${year.toString().padStart(4, "0")}-${month.toString().padStart(2, "0")}-${day.toString().padStart(2, "0")}`;
}

export function validateImport(input: { businessId: string; table: ParsedTable; mapping: HeaderMapping; profile: ImportProfile }): ImportResult {
  const { businessId, table, mapping, profile } = input;
  const issues: ImportIssue[] = [];
  const required: CanonicalField[] = ["customerExternalId", "customerName", "chargeExternalId", "amount", "description", "dueDate"];
  for (const field of required) if (!mapping[field]) issues.push({ row: 1, field, message: "Asigna una columna a este campo obligatorio." });
  if (!businessId.trim()) issues.push({ row: 1, field: "file", message: "Selecciona el negocio destino." });

  const rows: CanonicalImportRow[] = [];
  const chargeIds = new Set<string>();
  table.rows.forEach((source, index) => {
    const sourceRow = index + 2;
    const rowIssues: ImportIssue[] = [];
    const requiredValue = (field: CanonicalField, label: string) => {
      const value = mappedValue(source, mapping, field);
      if (!value) rowIssues.push({ row: sourceRow, field, message: `${label} es obligatorio.` });
      return value;
    };
    const customerExternalId = requiredValue("customerExternalId", "La clave del cliente");
    const customerName = requiredValue("customerName", "El nombre del cliente");
    const chargeExternalId = requiredValue("chargeExternalId", "La referencia del cobro");
    const amountText = requiredValue("amount", "El monto");
    const description = requiredValue("description", "El concepto");
    const dueDateText = requiredValue("dueDate", "La fecha de vencimiento");
    const email = mappedValue(source, mapping, "customerEmail").toLocaleLowerCase("es-MX") || null;
    const amountMinor = parseAmountMinor(amountText, profile.decimalSeparator);
    const dueDate = normalizedDate(dueDateText, profile.dateFormat);
    if (amountText && amountMinor === null) rowIssues.push({ row: sourceRow, field: "amount", message: "Usa un monto positivo con máximo dos decimales." });
    if (dueDateText && dueDate === null) rowIssues.push({ row: sourceRow, field: "dueDate", message: `La fecha no coincide con el formato ${profile.dateFormat.toUpperCase()}.` });
    if (email && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)) rowIssues.push({ row: sourceRow, field: "customerEmail", message: "El correo no tiene un formato válido." });
    if (chargeExternalId && chargeIds.has(chargeExternalId)) rowIssues.push({ row: sourceRow, field: "chargeExternalId", message: "La referencia del cobro está duplicada en este archivo." });
    if (chargeExternalId) chargeIds.add(chargeExternalId);
    issues.push(...rowIssues);
    if (rowIssues.length === 0 && required.every((field) => mapping[field]) && businessId.trim() && amountMinor !== null && dueDate) {
      rows.push({
        businessId: businessId.trim(), sourceRow,
        customer: { externalId: customerExternalId, displayName: customerName.replace(/\s+/g, " "), email },
        charge: { externalId: chargeExternalId, amountMinor, currency: profile.currency, description: description.replace(/\s+/g, " "), dueDate },
      });
    }
  });
  const errorRows = new Set(issues.filter((issue) => issue.row > 1).map((issue) => issue.row)).size;
  return {
    rows, issues,
    summary: { inputRows: table.rows.length, validRows: rows.length, errorRows, totalMinor: rows.reduce((total, row) => total + row.charge.amountMinor, 0) },
  };
}
