import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from payments import imports


def _record(source_row=2, external_id="CLI-1", name="Ferretería del Norte", email="cobros@example.com",
            charge_id="FAC-1", amount_minor=1250000, currency="MXN", description="Material", due_date="2026-09-30"):
    return {
        "sourceRow": source_row,
        "customer": {"externalId": external_id, "displayName": name, "email": email},
        "charge": {"externalId": charge_id, "amountMinor": amount_minor, "currency": currency, "description": description, "dueDate": due_date},
    }


def _source():
    return {"fileName": "cartera.csv", "format": "csv"}


def _profile():
    return {"name": "Cobranza estándar MX", "delimiter": "comma", "dateFormat": "iso", "decimalSeparator": "dot", "currency": "MXN"}


# --- record-level validation --------------------------------------------

def test_a_fully_valid_record_normalizes_cleanly():
    normalized, issues = imports.normalize_record(_record(), set())
    assert issues == []
    assert normalized == {
        "sourceRow": 2,
        "customer": {"externalId": "CLI-1", "displayName": "Ferretería del Norte", "email": "cobros@example.com"},
        "charge": {"externalId": "FAC-1", "amountMinor": 1250000, "currency": "MXN", "description": "Material", "dueDate": "2026-09-30"},
    }


def test_amount_minor_bool_is_invalid():
    normalized, issues = imports.normalize_record(_record(amount_minor=True), set())
    assert normalized is None
    assert any(issue["field"] == "amount" for issue in issues)


def test_amount_minor_float_is_invalid():
    normalized, issues = imports.normalize_record(_record(amount_minor=1250.5), set())
    assert normalized is None
    assert any(issue["field"] == "amount" for issue in issues)


def test_amount_minor_zero_or_negative_is_invalid():
    for amount in (0, -100):
        normalized, issues = imports.normalize_record(_record(amount_minor=amount), set())
        assert normalized is None
        assert any(issue["field"] == "amount" for issue in issues)


def test_amount_minor_above_the_pilot_ceiling_is_invalid():
    normalized, issues = imports.normalize_record(_record(amount_minor=imports.MAX_AMOUNT_MINOR + 1), set())
    assert normalized is None
    assert any(issue["field"] == "amount" for issue in issues)


def test_amount_minor_at_the_pilot_ceiling_is_valid():
    normalized, issues = imports.normalize_record(_record(amount_minor=imports.MAX_AMOUNT_MINOR), set())
    assert issues == []
    assert normalized["charge"]["amountMinor"] == imports.MAX_AMOUNT_MINOR


def test_impossible_date_is_invalid():
    normalized, issues = imports.normalize_record(_record(due_date="2026-02-31"), set())
    assert normalized is None
    assert any(issue["field"] == "dueDate" for issue in issues)


def test_non_iso_date_format_is_invalid():
    normalized, issues = imports.normalize_record(_record(due_date="30/09/2026"), set())
    assert normalized is None
    assert any(issue["field"] == "dueDate" for issue in issues)


def test_invalid_email_is_rejected_but_email_is_optional():
    normalized, issues = imports.normalize_record(_record(email="not-an-email"), set())
    assert normalized is None
    assert any(issue["field"] == "customerEmail" for issue in issues)
    normalized, issues = imports.normalize_record(_record(email=None), set())
    assert issues == []
    assert normalized["customer"]["email"] is None


def test_email_is_normalized_to_lowercase():
    normalized, _ = imports.normalize_record(_record(email="Cobros@Example.COM"), set())
    assert normalized["customer"]["email"] == "cobros@example.com"


def test_duplicate_charge_external_id_within_the_batch_is_rejected():
    seen = set()
    first, first_issues = imports.normalize_record(_record(source_row=2, charge_id="FAC-1"), seen)
    second, second_issues = imports.normalize_record(_record(source_row=3, charge_id="FAC-1"), seen)
    assert first_issues == []
    assert first is not None
    assert second is None
    assert any(issue["field"] == "chargeExternalId" and "duplicada" in issue["message"] for issue in second_issues)


def test_currency_other_than_mxn_is_rejected():
    normalized, issues = imports.normalize_record(_record(currency="USD"), set())
    assert normalized is None
    assert any(issue["field"] == "currency" for issue in issues)


def test_missing_required_fields_are_all_reported():
    normalized, issues = imports.normalize_record({"sourceRow": 2, "customer": {}, "charge": {}}, set())
    assert normalized is None
    fields = {issue["field"] for issue in issues}
    assert {"customerExternalId", "customerName", "chargeExternalId", "amount", "currency", "description", "dueDate"} <= fields


def test_non_positive_or_non_integer_source_row_is_invalid():
    for bad in (0, -1, "2", 2.5, None):
        normalized, issues = imports.normalize_record(_record(source_row=bad), set())
        assert normalized is None
        assert any(issue["field"] == "sourceRow" for issue in issues)


def test_oversized_strings_are_rejected():
    normalized, issues = imports.normalize_record(_record(external_id="X" * (imports.MAX_EXTERNAL_ID_LEN + 1)), set())
    assert normalized is None
    assert any(issue["field"] == "customerExternalId" for issue in issues)
    normalized, issues = imports.normalize_record(_record(description="X" * (imports.MAX_DESCRIPTION_LEN + 1)), set())
    assert normalized is None
    assert any(issue["field"] == "description" for issue in issues)


def test_whitespace_is_collapsed_deterministically():
    normalized, _ = imports.normalize_record(_record(name="  Ferretería   del   Norte  "), set())
    assert normalized["customer"]["displayName"] == "Ferretería del Norte"


def test_a_record_that_is_not_an_object_is_rejected_without_crashing():
    normalized, issues = imports.normalize_record("not-a-dict", set())
    assert normalized is None
    assert issues


# --- batch-level validation ----------------------------------------------

def test_empty_batch_is_rejected():
    with pytest.raises(ValueError):
        imports.validate_batch([])


def test_batch_exceeding_the_pilot_row_limit_is_rejected():
    with pytest.raises(ValueError):
        imports.validate_batch([_record(source_row=n, charge_id=f"FAC-{n}") for n in range(2, imports.MAX_RECORDS_PER_IMPORT + 3)])


def test_batch_at_exactly_the_pilot_row_limit_is_accepted():
    records = [_record(source_row=n, charge_id=f"FAC-{n}") for n in range(2, imports.MAX_RECORDS_PER_IMPORT + 2)]
    result = imports.validate_batch(records)
    assert result["summary"]["inputRows"] == imports.MAX_RECORDS_PER_IMPORT


def test_a_clean_batch_is_validated_with_no_issues():
    result = imports.validate_batch([_record()])
    assert result["status"] == "validated"
    assert result["summary"] == {"inputRows": 1, "validRows": 1, "errorRows": 0, "totalMinor": 1250000}
    assert result["issues"] == []
    assert result["issuesTruncated"] is False


def test_a_batch_with_any_invalid_row_is_marked_invalid_overall():
    result = imports.validate_batch([_record(), _record(source_row=3, charge_id="FAC-2", amount_minor=-5)])
    assert result["status"] == "invalid"
    assert result["summary"]["validRows"] == 1
    assert result["summary"]["errorRows"] == 1
    # Money from the invalid row is never counted.
    assert result["summary"]["totalMinor"] == 1250000


def test_error_counts_survive_issue_sample_truncation():
    records = [_record(source_row=n, charge_id=f"FAC-{n}", amount_minor=-1) for n in range(2, 2 + imports.MAX_ISSUES_SAMPLE + 10)]
    result = imports.validate_batch(records)
    assert result["issuesTruncated"] is True
    assert len(result["issues"]) == imports.MAX_ISSUES_SAMPLE
    # The full error count is never truncated, only the sample shown/stored.
    assert result["summary"]["errorRows"] == len(records)


def test_chunk_rows_never_exceeds_the_configured_chunk_size():
    row_results = [{"sourceRow": n, "valid": True, "normalized": {}, "issues": []} for n in range(1, 63)]
    chunks = imports.chunk_rows(row_results)
    assert all(len(chunk) <= imports.ROWS_PER_CHUNK for chunk in chunks)
    assert sum(len(chunk) for chunk in chunks) == len(row_results)


# --- source/profile structural validation ---------------------------------

def test_source_format_must_be_a_known_value():
    with pytest.raises(ValueError):
        imports.validate_source({"fileName": "x.xlsx", "format": "xlsx"})
    assert imports.validate_source(_source())["format"] == "csv"


def test_profile_enums_are_checked_against_the_frontends_own_set():
    for field, bad in (("delimiter", "pipe"), ("dateFormat", "yyyy"), ("decimalSeparator", "space")):
        profile = _profile()
        profile[field] = bad
        with pytest.raises(ValueError):
            imports.validate_profile(profile)


def test_profile_currency_must_be_mxn():
    profile = _profile()
    profile["currency"] = "USD"
    with pytest.raises(ValueError):
        imports.validate_profile(profile)


def test_profile_name_is_required():
    profile = _profile()
    profile["name"] = "   "
    with pytest.raises(ValueError):
        imports.validate_profile(profile)


# --- digest determinism ----------------------------------------------------

def test_digest_is_stable_for_an_equivalent_payload():
    records_a = [_record(email="Cobros@Example.com", name="  Ferretería   del Norte ")]
    records_b = [_record(email="cobros@example.com", name="Ferretería del Norte")]
    assert imports.canonical_digest(_source(), _profile(), records_a) == imports.canonical_digest(_source(), _profile(), records_b)


def test_digest_changes_when_the_payload_actually_differs():
    base = imports.canonical_digest(_source(), _profile(), [_record()])
    different_amount = imports.canonical_digest(_source(), _profile(), [_record(amount_minor=999)])
    different_file = imports.canonical_digest({"fileName": "other.csv", "format": "csv"}, _profile(), [_record()])
    assert base != different_amount
    assert base != different_file


def test_digest_is_stable_across_repeated_calls():
    records = [_record(), _record(source_row=3, charge_id="FAC-2")]
    first = imports.canonical_digest(_source(), _profile(), records)
    second = imports.canonical_digest(_source(), _profile(), records)
    assert first == second


# --- Hallazgo 2: rowResults.sourceRow is always DynamoDB-safe --------------

@pytest.mark.parametrize("bad_source_row", [2.5, True, "2", [2], {"n": 2}, None, -1, 0])
def test_row_results_source_row_is_never_the_raw_unsafe_value(bad_source_row):
    result = imports.validate_batch([_record(source_row=bad_source_row)])
    assert result["status"] == "invalid"
    stored = result["rowResults"][0]["sourceRow"]
    assert stored == 0
    assert isinstance(stored, int) and not isinstance(stored, bool)


def test_row_results_source_row_is_the_validated_int_when_the_row_is_valid():
    result = imports.validate_batch([_record(source_row=7)])
    assert result["rowResults"][0]["sourceRow"] == 7
    assert isinstance(result["rowResults"][0]["sourceRow"], int)


def test_row_results_source_row_is_zero_when_only_a_non_source_row_field_is_invalid():
    # sourceRow=3 is itself fine, but amountMinor is a float -- the whole
    # row is invalid, so nothing about it (including its own otherwise-good
    # sourceRow) is trusted enough to store as a real row number.
    result = imports.validate_batch([_record(source_row=3, amount_minor=12.5)])
    assert result["status"] == "invalid"
    assert result["rowResults"][0]["sourceRow"] == 0


def test_source_row_above_the_maximum_is_invalid():
    normalized, issues = imports.normalize_record(_record(source_row=imports.MAX_SOURCE_ROW + 1), set())
    assert normalized is None
    assert any(issue["field"] == "sourceRow" for issue in issues)


def test_source_row_at_the_maximum_is_valid():
    normalized, issues = imports.normalize_record(_record(source_row=imports.MAX_SOURCE_ROW), set())
    assert issues == []
    assert normalized["sourceRow"] == imports.MAX_SOURCE_ROW


# --- Hallazgo 8: ambiguous customer.externalId within a batch --------------

def test_same_customer_identity_across_two_charges_is_not_a_conflict():
    seen_charges, seen_identities = set(), {}
    first, first_issues = imports.normalize_record(_record(source_row=2, name="Empresa Uno", email="a@example.com", charge_id="FAC-1"), seen_charges, seen_identities)
    second, second_issues = imports.normalize_record(_record(source_row=3, name="Empresa Uno", email="a@example.com", charge_id="FAC-2"), seen_charges, seen_identities)
    assert first_issues == [] and second_issues == []
    assert first is not None and second is not None


def test_same_external_id_different_display_name_is_a_conflict():
    seen_charges, seen_identities = set(), {}
    imports.normalize_record(_record(source_row=2, name="Empresa Uno", charge_id="FAC-1"), seen_charges, seen_identities)
    second, issues = imports.normalize_record(_record(source_row=3, name="Otra Empresa", charge_id="FAC-2"), seen_charges, seen_identities)
    assert second is None
    assert any(issue["field"] == "customerExternalId" and "fila 2" in issue["message"] for issue in issues)


def test_same_external_id_different_email_is_a_conflict():
    seen_charges, seen_identities = set(), {}
    imports.normalize_record(_record(source_row=2, email="a@example.com", charge_id="FAC-1"), seen_charges, seen_identities)
    second, issues = imports.normalize_record(_record(source_row=3, email="b@example.com", charge_id="FAC-2"), seen_charges, seen_identities)
    assert second is None
    assert any(issue["field"] == "customerExternalId" for issue in issues)


def test_email_normalization_prevents_a_false_identity_conflict():
    seen_charges, seen_identities = set(), {}
    imports.normalize_record(_record(source_row=2, email=" A@Example.COM ", charge_id="FAC-1"), seen_charges, seen_identities)
    second, issues = imports.normalize_record(_record(source_row=3, email="a@example.com", charge_id="FAC-2"), seen_charges, seen_identities)
    assert issues == []
    assert second is not None


def test_a_row_omitting_email_does_not_conflict_with_an_earlier_row_that_had_one():
    seen_charges, seen_identities = set(), {}
    imports.normalize_record(_record(source_row=2, email="a@example.com", charge_id="FAC-1"), seen_charges, seen_identities)
    second, issues = imports.normalize_record(_record(source_row=3, email=None, charge_id="FAC-2"), seen_charges, seen_identities)
    assert issues == []
    assert second is not None


def test_validate_batch_marks_an_identity_conflict_invalid_overall():
    records = [
        _record(source_row=2, name="Empresa Uno", charge_id="FAC-1"),
        _record(source_row=3, name="Otra Empresa", charge_id="FAC-2"),
    ]
    result = imports.validate_batch(records)
    assert result["status"] == "invalid"
    assert any(issue["field"] == "customerExternalId" for issue in result["issues"])
