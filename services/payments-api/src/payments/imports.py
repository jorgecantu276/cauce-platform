"""Pure validation/domain rules for platform ETL import jobs.

No I/O here (no DynamoDB, no HTTP). `platform.py` orchestrates this module
against the repository; `platform_transport.py` never calls it directly.

`validated` means the batch satisfies this contract -- not that any
Customer or Charge was created. Only the explicit apply operation
(DynamoRepository.apply_import_job) turns a validated job into Customers and
Charges; see docs/delivery/2026-09-19-etl-retention-and-apply.md.
"""

import hashlib
import json
import re
import uuid
from datetime import date


# validated -> applying -> applied | review;  validated|invalid|applied|review|
# applying(no live lease) -> purging -> (purged: only as audit evidence, the
# metadata item no longer exists). See the delivery doc's state machine.
IMPORT_STATES = ("validated", "invalid", "applying", "applied", "review", "purging")

# Pilot retention for the temporary row chunks (customer PII). 30 days is a
# sandbox pilot setting, NOT a legal/compliance decision -- it is configured
# through the ImportRowsRetentionDays template parameter, never hardcoded at
# the call site.
DEFAULT_IMPORT_ROWS_RETENTION_DAYS = 30
MIN_IMPORT_ROWS_RETENTION_DAYS = 1
MAX_IMPORT_ROWS_RETENTION_DAYS = 365


def validate_retention_days(value):
    """The configured retention, as a plain int within the pilot bounds.
    Refuses (rather than silently defaulting) anything else: a misconfigured
    retention must fail loudly at startup, not quietly keep PII longer or
    expire it sooner than the operator asked."""
    if not isinstance(value, int) or isinstance(value, bool) or not (
        MIN_IMPORT_ROWS_RETENTION_DAYS <= value <= MAX_IMPORT_ROWS_RETENTION_DAYS
    ):
        raise ValueError(
            f"import rows retention must be an integer between {MIN_IMPORT_ROWS_RETENTION_DAYS} "
            f"and {MAX_IMPORT_ROWS_RETENTION_DAYS} days"
        )
    return value


class ImportIdempotencyConflict(Exception):
    """Same (business, subject, Idempotency-Key) reused with a different
    payload. A distinct type on purpose (not a plain ValueError): callers
    must be able to tell "your request is malformed" (400) apart from "this
    exact retry doesn't match what you sent before" (409) without parsing
    exception text. Raised by DynamoRepository.create_import_job, not by
    this module -- it lives here because both dynamodb.py (raises it) and
    platform_transport.py (catches it) already depend on this module, and a
    third shared location would only add an import for one class."""

class ImportOperationRefused(Exception):
    """A lifecycle operation (apply/purge) that the import's current state
    does not allow, e.g. apply on an `invalid`/`purged`/already-`applied`
    job, or purge while an apply holds a live lease. A distinct type -- not a
    ValueError -- so the transport can answer 409 with a stable, non-PII
    `code` instead of the generic 400 it uses for malformed requests."""

    def __init__(self, code, status=None):
        super().__init__(code)
        self.code = code
        self.status = status


# Pilot limits -- deliberately conservative and documented rather than
# derived from a single "whatever fits in 400KB" calculation. See the
# delivery doc for the exact math (rows/chunk * chunk size, DynamoDB's
# 100-item/4MB TransactWriteItems ceiling).
MAX_RECORDS_PER_IMPORT = 500
ROWS_PER_CHUNK = 25
MAX_ISSUES_SAMPLE = 50
MAX_EXTERNAL_ID_LEN = 120
MAX_DISPLAY_NAME_LEN = 160
MAX_EMAIL_LEN = 254
MAX_DESCRIPTION_LEN = 240
MAX_FILE_NAME_LEN = 200
MAX_PROFILE_NAME_LEN = 120
MAX_AMOUNT_MINOR = 500_000_000  # $5,000,000.00 MXN, a generous pilot ceiling
MAX_SOURCE_ROW = 100_000  # far beyond MAX_RECORDS_PER_IMPORT; guards against an absurd/adversarial value reaching storage

SOURCE_FORMATS = {"csv", "tsv", "paste"}
DELIMITERS = {"auto", "tab", "comma", "semicolon"}
DATE_FORMATS = {"iso", "dmy", "mdy"}
DECIMAL_SEPARATORS = {"dot", "comma"}

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _issue(row, field, message):
    return {"row": row, "field": field, "message": message}


def _clean_str(value):
    return re.sub(r"\s+", " ", str(value or "").strip())


def validate_source(source):
    """Structural checks for the `source` envelope. Unknown extra keys are
    ignored deliberately (documented policy, not an oversight): this
    receives the frontend's own canonical payload, and rejecting on an
    unrecognized additional field would make a harmless client-side version
    bump a hard failure for every import."""
    if not isinstance(source, dict):
        raise ValueError("source must be an object")
    file_name = source.get("fileName")
    if file_name is not None and (not isinstance(file_name, str) or len(file_name) > MAX_FILE_NAME_LEN):
        raise ValueError("source.fileName must be a string within the pilot length limit")
    fmt = source.get("format")
    if fmt not in SOURCE_FORMATS:
        raise ValueError("source.format must be one of: " + ", ".join(sorted(SOURCE_FORMATS)))
    return {"fileName": _clean_str(file_name)[:MAX_FILE_NAME_LEN], "format": fmt}


def validate_profile(profile):
    """Structural checks for the `profile` envelope, mirroring the frontend's
    own ImportProfile type exactly so the server never accepts an option the
    UI could not have produced."""
    if not isinstance(profile, dict):
        raise ValueError("profile must be an object")
    name = _clean_str(profile.get("name"))
    if not name or len(name) > MAX_PROFILE_NAME_LEN:
        raise ValueError("profile.name is required and must fit the pilot length limit")
    delimiter = profile.get("delimiter")
    if delimiter not in DELIMITERS:
        raise ValueError("profile.delimiter must be one of: " + ", ".join(sorted(DELIMITERS)))
    date_format = profile.get("dateFormat")
    if date_format not in DATE_FORMATS:
        raise ValueError("profile.dateFormat must be one of: " + ", ".join(sorted(DATE_FORMATS)))
    decimal_separator = profile.get("decimalSeparator")
    if decimal_separator not in DECIMAL_SEPARATORS:
        raise ValueError("profile.decimalSeparator must be one of: " + ", ".join(sorted(DECIMAL_SEPARATORS)))
    currency = profile.get("currency")
    if currency != "MXN":
        raise ValueError("profile.currency must be MXN for this pilot")
    return {
        "name": name, "delimiter": delimiter, "dateFormat": date_format,
        "decimalSeparator": decimal_separator, "currency": currency,
    }


def normalize_record(record, seen_charge_external_ids, seen_customer_identities=None):
    """Validate and normalize exactly one record. Returns (normalized, issues):
    `normalized` is None whenever `issues` is non-empty -- an invalid row is
    never partially applied. `seen_charge_external_ids`/`seen_customer_identities`
    are mutated to catch in-batch duplicate charge references and ambiguous
    customer identities, mirroring import-engine.ts's own dry-run logic
    (server-side is at least as strict, never looser). `seen_customer_identities`
    defaults to a fresh dict so this function stays usable standalone for a
    single record; validate_batch always passes one shared across its loop."""
    if seen_customer_identities is None:
        seen_customer_identities = {}
    issues = []
    if not isinstance(record, dict):
        return None, [_issue(0, "file", "Cada registro debe ser un objeto JSON.")]

    source_row = record.get("sourceRow")
    if not isinstance(source_row, int) or isinstance(source_row, bool) or source_row <= 0:
        issues.append(_issue(0, "sourceRow", "sourceRow debe ser un entero positivo."))
        row = 0
    elif source_row > MAX_SOURCE_ROW:
        issues.append(_issue(0, "sourceRow", f"sourceRow excede el máximo de {MAX_SOURCE_ROW}."))
        row = 0
    else:
        row = source_row

    customer = record.get("customer") if isinstance(record.get("customer"), dict) else {}
    charge = record.get("charge") if isinstance(record.get("charge"), dict) else {}

    external_id = _clean_str(customer.get("externalId"))
    if not external_id:
        issues.append(_issue(row, "customerExternalId", "La clave del cliente es obligatoria."))
    elif len(external_id) > MAX_EXTERNAL_ID_LEN:
        issues.append(_issue(row, "customerExternalId", f"La clave del cliente excede {MAX_EXTERNAL_ID_LEN} caracteres."))

    display_name = _clean_str(customer.get("displayName"))
    if not display_name:
        issues.append(_issue(row, "customerName", "El nombre del cliente es obligatorio."))
    elif len(display_name) > MAX_DISPLAY_NAME_LEN:
        issues.append(_issue(row, "customerName", f"El nombre del cliente excede {MAX_DISPLAY_NAME_LEN} caracteres."))

    email_raw = customer.get("email")
    email = None
    if email_raw not in (None, ""):
        candidate = str(email_raw).strip().lower()
        if len(candidate) > MAX_EMAIL_LEN or not EMAIL_RE.fullmatch(candidate):
            issues.append(_issue(row, "customerEmail", "El correo no tiene un formato válido."))
        else:
            email = candidate

    # Identity policy for a repeated customer.externalId within the batch:
    # displayName must match exactly (already whitespace/case-normalized
    # above); email only conflicts when BOTH occurrences provide one and
    # they differ -- a row that simply omits the email is never treated as
    # contradicting an earlier row that had one, and a later row that adds
    # an email no earlier row had enriches the record instead of
    # conflicting with it. This only compares occurrences *within this
    # batch*; it never checks against already-stored Customers (that is
    # explicitly deferred to a future preflight/apply step -- see the
    # delivery doc).
    if external_id:
        seen = seen_customer_identities.get(external_id)
        if seen is None:
            seen_customer_identities[external_id] = (row, display_name, email)
        else:
            first_row, first_name, first_email = seen
            if first_name != display_name or (first_email and email and first_email != email):
                issues.append(_issue(row, "customerExternalId", f"La clave del cliente ya apareció en la fila {first_row} con nombre o correo distinto."))
            elif not first_email and email:
                seen_customer_identities[external_id] = (first_row, first_name, email)

    charge_external_id = _clean_str(charge.get("externalId"))
    if not charge_external_id:
        issues.append(_issue(row, "chargeExternalId", "La referencia del cobro es obligatoria."))
    else:
        if len(charge_external_id) > MAX_EXTERNAL_ID_LEN:
            issues.append(_issue(row, "chargeExternalId", f"La referencia del cobro excede {MAX_EXTERNAL_ID_LEN} caracteres."))
        if charge_external_id in seen_charge_external_ids:
            issues.append(_issue(row, "chargeExternalId", "La referencia del cobro está duplicada en este lote."))
        seen_charge_external_ids.add(charge_external_id)

    amount_minor = charge.get("amountMinor")
    # No floats for money, ever: bool is a subclass of int, so it is
    # rejected explicitly rather than silently coerced to 0/1 minor units.
    if not isinstance(amount_minor, int) or isinstance(amount_minor, bool):
        issues.append(_issue(row, "amount", "El monto debe ser un entero de centavos, sin decimales."))
    elif amount_minor <= 0:
        issues.append(_issue(row, "amount", "El monto debe ser mayor que cero."))
    elif amount_minor > MAX_AMOUNT_MINOR:
        issues.append(_issue(row, "amount", "El monto excede el máximo permitido para el piloto."))

    currency = charge.get("currency")
    if currency != "MXN":
        issues.append(_issue(row, "currency", "Solo se admite MXN en este piloto."))

    description = _clean_str(charge.get("description"))
    if not description:
        issues.append(_issue(row, "description", "El concepto es obligatorio."))
    elif len(description) > MAX_DESCRIPTION_LEN:
        issues.append(_issue(row, "description", f"El concepto excede {MAX_DESCRIPTION_LEN} caracteres."))

    due_date_raw = charge.get("dueDate")
    if not isinstance(due_date_raw, str) or not ISO_DATE_RE.fullmatch(due_date_raw):
        issues.append(_issue(row, "dueDate", "La fecha de vencimiento debe tener formato AAAA-MM-DD."))
    else:
        try:
            date.fromisoformat(due_date_raw)
        except ValueError:
            issues.append(_issue(row, "dueDate", "La fecha de vencimiento no es una fecha real."))

    if issues:
        return None, issues

    normalized = {
        "sourceRow": source_row,
        "customer": {"externalId": external_id, "displayName": display_name, "email": email},
        "charge": {
            "externalId": charge_external_id, "amountMinor": amount_minor, "currency": currency,
            "description": description, "dueDate": due_date_raw,
        },
    }
    return normalized, []


def validate_batch(records):
    """Validate every record in the batch. Raises ValueError for
    structural/size problems that make the whole request unprocessable
    (rejected before any DynamoDB transaction is attempted); returns a
    result dict for anything that produces a `validated`/`invalid` job."""
    if not isinstance(records, list):
        raise ValueError("records must be a list")
    if len(records) == 0:
        raise ValueError("the batch cannot be empty")
    if len(records) > MAX_RECORDS_PER_IMPORT:
        raise ValueError(f"the batch exceeds the pilot limit of {MAX_RECORDS_PER_IMPORT} rows")

    seen_charge_external_ids = set()
    seen_customer_identities = {}
    row_results = []
    all_issues = []
    for raw in records:
        normalized, issues = normalize_record(raw, seen_charge_external_ids, seen_customer_identities)
        # Never the raw request value here, validated or not: it can be a
        # float, bool, string, list, or arbitrarily large int, none of
        # which boto3 will accept when this later reaches DynamoDB (a
        # float raises TypeError and aborts the whole transaction). The
        # only two values that ever reach storage are the validated
        # positive int (row is genuinely valid) or a plain 0 (it is not) --
        # matching the same "row=0 means no reliable row number" contract
        # the issues list already uses.
        safe_source_row = normalized["sourceRow"] if normalized is not None else 0
        row_results.append({"sourceRow": safe_source_row, "valid": normalized is not None, "normalized": normalized, "issues": issues})
        all_issues.extend(issues)

    error_rows = sum(1 for row in row_results if not row["valid"])
    valid_rows = len(row_results) - error_rows
    total_minor = sum(row["normalized"]["charge"]["amountMinor"] for row in row_results if row["valid"])
    issues_truncated = len(all_issues) > MAX_ISSUES_SAMPLE
    summary = {"inputRows": len(records), "validRows": valid_rows, "errorRows": error_rows, "totalMinor": total_minor}
    return {
        "status": "invalid" if error_rows > 0 else "validated",
        "summary": summary,
        "issues": all_issues[:MAX_ISSUES_SAMPLE],
        "issuesTruncated": issues_truncated,
        "rowResults": row_results,
    }


def chunk_rows(row_results):
    """Split row results into bounded chunks for storage. Never one item
    per row (too many transactional items for a large batch) and never the
    whole batch in one item (risks the 400KB item-size ceiling)."""
    return [row_results[index:index + ROWS_PER_CHUNK] for index in range(0, len(row_results), ROWS_PER_CHUNK)]


def _canonical_record(record):
    customer = record.get("customer") if isinstance(record, dict) and isinstance(record.get("customer"), dict) else {}
    charge = record.get("charge") if isinstance(record, dict) and isinstance(record.get("charge"), dict) else {}
    return {
        "sourceRow": record.get("sourceRow") if isinstance(record, dict) else None,
        "customer": {
            "externalId": _clean_str(customer.get("externalId")),
            "displayName": _clean_str(customer.get("displayName")),
            "email": (str(customer.get("email")).strip().lower() or None) if customer.get("email") not in (None, "") else None,
        },
        "charge": {
            "externalId": _clean_str(charge.get("externalId")),
            "amountMinor": charge.get("amountMinor"),
            "currency": charge.get("currency"),
            "description": _clean_str(charge.get("description")),
            "dueDate": charge.get("dueDate"),
        },
    }


def canonical_digest(source, profile, records):
    """Deterministic digest of the meaningful request content, computed
    after the same whitespace/case normalization every record goes through
    during validation -- so two requests that differ only in incidental
    formatting (extra spaces, email casing) hash identically, while any
    difference that would change validation output also changes the digest.
    Unrecognized extra top-level/nested keys never reach here (see
    validate_source/validate_profile/normalize_record's documented policy
    of ignoring them), so they cannot be used to force a digest collision
    or divergence."""
    canonical = {
        "source": {"fileName": _clean_str((source or {}).get("fileName")), "format": (source or {}).get("format")},
        "profile": {
            "name": _clean_str((profile or {}).get("name")),
            "delimiter": (profile or {}).get("delimiter"),
            "dateFormat": (profile or {}).get("dateFormat"),
            "decimalSeparator": (profile or {}).get("decimalSeparator"),
            "currency": (profile or {}).get("currency"),
        },
        "records": [_canonical_record(record) for record in (records or [])],
    }
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


# --- Apply: deterministic identity and per-row decisions ---------------------
#
# An imported Customer/Charge gets an id derived ONLY from
# (businessId, kind, externalId), never a random uuid. That single choice is
# what makes apply idempotent and race-safe: the same external record always
# maps to the same DynamoDB key, so "create it" is a conditional Put on that
# key -- a retry, a second job, or a concurrent worker can never create a
# second copy, and "does it already exist" needs no search.

# Fixed namespace for uuid5. Not a secret; it only has to never change (a
# different value would map every external id to a different record).
IMPORT_ID_NAMESPACE = uuid.UUID("6f0c2b0e-5d3a-4c39-9a52-3f1a7c1d8e42")

# Per-invocation apply bounds (see the delivery doc, section 3 point 4).
APPLY_ROWS_PER_INVOCATION = 50
APPLY_TIME_BUDGET_SECONDS = 5.0
APPLY_LEASE_SECONDS = 30
MAX_CONFLICT_SAMPLE = 50


def _identity_id(business_id, kind, external_id):
    # JSON-encoded rather than joined with a delimiter character: an external
    # id containing that delimiter must not be able to spell another
    # (business, kind, id) triple.
    return str(uuid.uuid5(IMPORT_ID_NAMESPACE, json.dumps([business_id, kind, external_id], separators=(",", ":"))))


def customer_id_for(business_id, external_id):
    return _identity_id(business_id, "customer", external_id)


def charge_id_for(business_id, external_id):
    return _identity_id(business_id, "charge", external_id)


def customer_identity_conflict(existing_name, existing_email, name, email):
    """Same policy validate_batch applies inside one batch, now against a
    stored Customer: the name must match exactly (both are whitespace-
    normalized); an email only conflicts when BOTH sides have one and they
    differ. A reused Customer is never modified -- no enrichment, no rename."""
    if _clean_str(existing_name) != name:
        return True
    existing = (str(existing_email).strip().lower() or None) if existing_email not in (None, "") else None
    return bool(existing and email and existing != email)


def _charge_matches(existing, customer_id, charge):
    return (
        existing.get("customer_id") == customer_id
        and int(existing["amount_minor"]) == charge["amountMinor"]
        and existing.get("currency") == charge["currency"]
        and existing.get("description") == charge["description"]
        and existing.get("due_date") == charge["dueDate"]
    )


def decide_row(record, existing_customer, existing_charge, customer_id):
    """What apply may do for one validated row, given what already exists
    under its deterministic keys. Returns {"conflict", "createCustomer",
    "createCharge"}. A conflict always means: write nothing for this row and
    never pick a side -- the operator decides."""
    def blocked(code):
        return {"conflict": code, "createCustomer": False, "createCharge": False}

    customer, charge = record["customer"], record["charge"]
    if existing_customer is not None and customer_identity_conflict(
        existing_customer.get("display_name"), existing_customer.get("email"), customer["displayName"], customer["email"],
    ):
        return blocked("customer_identity_conflict")
    if existing_charge is not None:
        if not _charge_matches(existing_charge, customer_id, charge):
            return blocked("charge_payload_conflict")
        if existing_charge.get("cancelled_at"):
            return blocked("charge_cancelled")
        if existing_customer is None:
            return blocked("customer_missing")
    return {"conflict": None, "createCustomer": existing_customer is None, "createCharge": existing_charge is None}


def revalidate_stored_rows(rows, summary):
    """Server-side re-check of the rows read back from storage, before apply
    writes anything: every row is a valid normalized record under the same
    rules validation used (over the WHOLE batch, so duplicate charge
    references and ambiguous customers are caught again), normalization is a
    no-op, and the count/total match the stored summary. Never trusts that
    storage still holds what validation wrote."""
    try:
        if not isinstance(rows, list) or not isinstance(summary, dict):
            return False
        if summary.get("errorRows") != 0 or summary.get("inputRows") != len(rows) or summary.get("validRows") != len(rows):
            return False
        seen_charge_ids, seen_customers, total = set(), {}, 0
        for stored in rows:
            if not isinstance(stored, dict) or stored.get("valid") is not True or stored.get("issues"):
                return False
            if not isinstance(stored.get("normalized"), dict):
                return False
            normalized, issues = normalize_record(stored["normalized"], seen_charge_ids, seen_customers)
            if issues or normalized != stored["normalized"]:
                return False
            total += normalized["charge"]["amountMinor"]
        return total == summary.get("totalMinor")
    except (KeyError, TypeError, ValueError):
        return False
