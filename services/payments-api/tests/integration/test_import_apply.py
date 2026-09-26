"""Controlled, idempotent, resumable ETL apply (Moto, real repository)."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
import json

from botocore.exceptions import ClientError
from moto import mock_aws
import pytest

from import_support import (
    BUSINESS_ID, NOW, OTHER_BUSINESS_ID, PII_EMAIL, PII_EXTERNAL_ID, PII_NAME, ROUTE_APPLY, SUPER,
    RealRuntime, all_items, create_job, dump, manifest, platform_event, record, records, table, validate_body,
)
from payments import imports
from payments.dynamodb import DynamoRepository
from payments.imports import ImportOperationRefused
from payments.platform import PlatformForbidden, PlatformService
from payments.platform_transport import handle_platform

SUBJECT = "subject-1"


def clock():
    return NOW


def repo_with_business(business_id=BUSINESS_ID):
    repo = DynamoRepository("payments", resource=table())
    repo.put_tenant_manifest(manifest(business_id))
    return repo


def apply(repo, import_id, key="apply-key-0001", subject=SUBJECT, business_id=BUSINESS_ID, **kwargs):
    # The wall-clock time budget is lifted by default so that tests asserting an
    # exact slice size depend only on the ROW bound: under a loaded machine a
    # real 5 s budget could end a slice early (47 rows instead of 50) and make
    # the test nondeterministic. The time bound itself has a dedicated test
    # with an injected fake clock (test_the_time_budget_bounds_a_slice...).
    kwargs.setdefault("time_budget_s", 1e9)
    return repo.apply_import_job(business_id, import_id, subject, key, kwargs.pop("clock", clock), **kwargs)


def business_items(repo, business_id=BUSINESS_ID):
    return [item for item in all_items(repo) if item["PK"] == f"BUSINESS#{business_id}"]


def by_prefix(repo, prefix, business_id=BUSINESS_ID):
    return [item for item in business_items(repo, business_id) if item["SK"].startswith(prefix)]


def customers(repo, business_id=BUSINESS_ID):
    return by_prefix(repo, "CUSTOMER#", business_id)


def charges(repo, business_id=BUSINESS_ID):
    return by_prefix(repo, "CHARGE#", business_id)


def snapshot(repo):
    return dump(all_items(repo))


class Spy:
    """Every TransactWriteItems call: (item count, payload bytes)."""
    def __init__(self, repo):
        self.calls = []
        real = repo.client.transact_write_items

        def spy(**kwargs):
            items = kwargs["TransactItems"]
            self.calls.append((len(items), len(json.dumps(items, default=str).encode())))
            return real(**kwargs)

        self._patch = patch.object(repo.client, "transact_write_items", side_effect=spy)

    def __enter__(self):
        self._patch.start()
        return self

    def __exit__(self, *exc):
        self._patch.stop()


# --- happy path ---------------------------------------------------------------

@mock_aws
def test_apply_of_a_validated_batch_creates_the_expected_customers_and_charges():
    repo = repo_with_business()
    rows = [
        record(2, "CLI-A", "Ferretería A", "a@example.com", "FAC-1", 100000, "Material", "2026-09-30"),
        record(3, "CLI-A", "Ferretería A", "a@example.com", "FAC-2", 250050, "Flete", "2026-10-15"),
        record(4, "CLI-B", "Transportes B", None, "FAC-3", 999, "Servicio", "2026-11-01"),
    ]
    job = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-a1", rows)
    result = apply(repo, job["importId"])

    assert result["status"] == "applied" and result["importId"] == job["importId"]
    assert result["apply"] == {
        "rowsTotal": 3, "rowsProcessed": 3, "customersCreated": 2, "customersReused": 1, "chargesCreated": 3,
        "chargesReused": 0, "conflicts": 0, "failed": 0, "leaseActive": False, "lastErrorCode": None,
        "startedAt": NOW.isoformat(), "completedAt": NOW.isoformat(), "conflictSample": [], "conflictsTruncated": False,
    }

    assert sorted((c["display_name"], c.get("email")) for c in customers(repo)) == [("Ferretería A", "a@example.com"), ("Transportes B", None)]
    by_external = {c["external_id"]: c for c in charges(repo)}
    assert sorted(by_external) == ["FAC-1", "FAC-2", "FAC-3"]
    first = by_external["FAC-2"]
    assert (int(first["amount_minor"]), first["currency"], first["description"], first["due_date"]) == (250050, "MXN", "Flete", "2026-10-15")
    for charge in charges(repo):
        assert int(charge["outstanding_minor"]) == int(charge["amount_minor"]) and int(charge["allocated_minor"]) == 0
        assert not charge.get("cancelled_at")
        assert isinstance(charge["amount_minor"], Decimal) and charge["amount_minor"] == int(charge["amount_minor"])  # whole minor units
    assert sorted(c["folio"] for c in charges(repo)) == ["CN-000001", "CN-000002", "CN-000003"]
    assert {c["customer_id"] for c in charges(repo) if c["external_id"] in ("FAC-1", "FAC-2")} == {imports.customer_id_for(BUSINESS_ID, "CLI-A")}

    # The normal read models see them as ordinary open receivables.
    assert repo.business_summary(BUSINESS_ID) == {"openChargeCount": 3, "outstandingMinor": 100000 + 250050 + 999}
    assert len(repo.list_customers(BUSINESS_ID)) == 2 and len(repo.list_charges(BUSINESS_ID)) == 3


@mock_aws
def test_apply_creates_only_customers_and_charges_never_any_financial_history():
    repo = repo_with_business()
    job = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-a2", records(6, customers=2))
    apply(repo, job["importId"])
    entities = {item.get("entity") for item in business_items(repo)}
    forbidden = {"payment", "allocation", "adjustment", "refund", "refund_lock", "attempt", "active_attempt", "link", "outbox",
                 "provider_event", "payment_identity", "review_resolution"}
    assert not (entities & forbidden)
    assert {"customer", "charge", "import_row_result"} <= entities
    assert not by_prefix(repo, "PAYMENT#") and not by_prefix(repo, "ALLOCATION#") and not by_prefix(repo, "LINK#") and not by_prefix(repo, "OUTBOX#")


@mock_aws
def test_imported_records_carry_a_deterministic_identity_and_their_origin():
    repo = repo_with_business()
    job = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-a3", [record(2, "CLI-Z", "Zeta", None, "FAC-Z", 500)])
    apply(repo, job["importId"])
    customer, charge = customers(repo)[0], charges(repo)[0]
    assert customer["id"] == imports.customer_id_for(BUSINESS_ID, "CLI-Z") and customer["SK"] == f"CUSTOMER#{customer['id']}"
    assert customer["external_id"] == "CLI-Z" and customer["origin"] == "import" and customer["import_id"] == job["importId"]
    assert charge["id"] == imports.charge_id_for(BUSINESS_ID, "FAC-Z") and charge["customer_id"] == customer["id"]
    assert charge["external_id"] == "FAC-Z" and charge["origin"] == "import" and charge["import_id"] == job["importId"]


@mock_aws
def test_apply_leaves_start_and_completion_audit_with_counts_and_no_personal_data():
    repo = repo_with_business()
    job = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-a4", [record(customer_id=PII_EXTERNAL_ID, name=PII_NAME, email=PII_EMAIL)])
    apply(repo, job["importId"], key="apply-key-audit")
    audits = {item["action"]: item for item in business_items(repo) if item.get("entity") == "audit"}
    assert {"import.validated", "import.apply_started", "import.applied"} <= set(audits)
    started, done = audits["import.apply_started"], audits["import.applied"]
    assert started["actor_id"] == SUBJECT and started["operation_key"] == "apply-key-audit" and started["aggregate_id"] == job["importId"]
    assert int(done["details"]["chargesCreated"]) == 1 and int(done["details"]["customersCreated"]) == 1 and int(done["details"]["conflicts"]) == 0
    audit_blob = dump([item for item in business_items(repo) if item.get("entity") in ("audit", "import_row_result", "import_job", "import_apply_key")])
    for secret in (PII_NAME, PII_EMAIL, PII_EXTERNAL_ID):
        assert secret not in audit_blob


# --- idempotency and replay -----------------------------------------------------

@mock_aws
def test_retrying_a_finished_apply_with_the_same_key_returns_the_same_result_and_writes_nothing():
    repo = repo_with_business()
    job = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-b1", records(5))
    first = apply(repo, job["importId"])
    before = snapshot(repo)
    later = lambda: NOW + timedelta(hours=3)
    assert apply(repo, job["importId"], clock=later) == first
    assert snapshot(repo) == before


@mock_aws
def test_a_second_idempotency_key_on_an_already_applied_job_is_refused_and_creates_nothing():
    repo = repo_with_business()
    job = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-b2", records(5))
    apply(repo, job["importId"])
    before = snapshot(repo)
    with pytest.raises(ImportOperationRefused) as refused:
        apply(repo, job["importId"], key="apply-key-OTHER")
    assert refused.value.code == "import_not_applicable" and refused.value.status == "applied"
    assert snapshot(repo) == before


@mock_aws
def test_a_repeated_key_after_a_partial_run_resumes_without_duplicating():
    repo = repo_with_business()
    job = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-b3", records(10, customers=4))
    first = apply(repo, job["importId"], max_rows=4)
    assert first["status"] == "applying" and first["apply"]["rowsProcessed"] == 4 and first["apply"]["leaseActive"] is False
    second = apply(repo, job["importId"], max_rows=4)
    assert second["apply"]["rowsProcessed"] == 8
    done = apply(repo, job["importId"], max_rows=4)
    assert done["status"] == "applied" and done["apply"]["rowsProcessed"] == 10
    assert len(charges(repo)) == 10 and len(customers(repo)) == 4
    assert sorted(c["folio"] for c in charges(repo)) == [f"CN-{n:06d}" for n in range(1, 11)]
    assert done["apply"]["customersCreated"] + done["apply"]["customersReused"] == 10
    assert done["apply"]["customersCreated"] == 4 and done["apply"]["chargesCreated"] == 10


@mock_aws
def test_progress_is_inspectable_between_slices_from_the_detail_endpoint():
    repo = repo_with_business()
    job = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-b4", records(120))
    seen = []
    for _ in range(3):
        view = apply(repo, job["importId"], max_rows=50)
        detail = repo.import_job_detail(BUSINESS_ID, job["importId"], NOW)
        assert detail == view
        seen.append((view["status"], view["apply"]["rowsProcessed"], view["apply"]["rowsTotal"]))
    assert seen == [("applying", 50, 120), ("applying", 100, 120), ("applied", 120, 120)]


@mock_aws
def test_the_time_budget_bounds_a_slice_even_when_row_count_would_allow_more():
    repo = repo_with_business()
    job = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-b5", records(20))
    ticks = iter(range(0, 1000))
    view = apply(repo, job["importId"], max_rows=50, time_budget_s=2.0, monotonic=lambda: float(next(ticks)))
    assert view["status"] == "applying" and 0 < view["apply"]["rowsProcessed"] < 20


# --- concurrency ------------------------------------------------------------------

@mock_aws
def test_a_second_apply_while_a_slice_is_running_does_not_process_or_duplicate():
    repo = repo_with_business()
    job = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-c1", records(6))
    outcome = {}
    original = repo._apply_import_row
    state = {"calls": 0}

    def wrapped(*args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 2:  # while A is mid-slice and holds the live lease
            before = len(by_prefix(repo, "IMPORT_APPLYROW#"))
            outcome["busy"] = apply(repo, job["importId"], key="apply-key-B")
            outcome["unchanged"] = len(by_prefix(repo, "IMPORT_APPLYROW#")) == before
        return original(*args, **kwargs)

    with patch.object(repo, "_apply_import_row", side_effect=wrapped):
        final = apply(repo, job["importId"], key="apply-key-A")

    assert outcome["busy"]["status"] == "applying" and outcome["busy"]["apply"]["leaseActive"] is True
    assert outcome["unchanged"] is True
    assert final["status"] == "applied" and len(charges(repo)) == 6 and len(customers(repo)) == 6


@mock_aws
def test_a_worker_whose_lease_was_taken_over_cannot_write_or_duplicate_anything():
    repo = repo_with_business()
    job = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-c2", records(6))
    original = repo._apply_import_row
    state = {"calls": 0, "b": None}

    def wrapped(*args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 3:  # A stalls; its lease expires; B takes over and finishes everything
            metadata = [i for i in business_items(repo) if i["SK"].startswith("IMPORT#")][0]
            repo.table.update_item(Key={"PK": metadata["PK"], "SK": metadata["SK"]},
                                   UpdateExpression="SET apply_lease_expires_at = :e", ExpressionAttributeValues={":e": (NOW - timedelta(seconds=1)).isoformat()})
            state["b"] = apply(repo, job["importId"], key="apply-key-B")
        return original(*args, **kwargs)

    with patch.object(repo, "_apply_import_row", side_effect=wrapped):
        a_view = apply(repo, job["importId"], key="apply-key-A")

    assert state["b"]["status"] == "applied"
    assert a_view["status"] == "applied"  # A stopped at its fence and reports the truth
    assert len(charges(repo)) == 6 and len(customers(repo)) == 6
    assert len(by_prefix(repo, "IMPORT_APPLYROW#")) == 6
    assert sorted(c["folio"] for c in charges(repo)) == [f"CN-{n:06d}" for n in range(1, 7)]
    assert [item["action"] for item in business_items(repo) if item.get("entity") == "audit"].count("import.applied") == 1


@mock_aws
def test_the_same_customer_racing_in_from_another_job_is_reused_never_duplicated():
    repo = repo_with_business()
    first = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-c3", [record(2, "CLI-1", "Uno", "u@example.com", "FAC-A", 100)])
    second = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-c4", [record(2, "CLI-1", "Uno", "u@example.com", "FAC-B", 200)])
    customer_sk = f"CUSTOMER#{imports.customer_id_for(BUSINESS_ID, 'CLI-1')}"
    real_get = repo._get
    state = {"stale": True}

    def stale_get(business_id, sk):
        # The second job decides while the customer does not exist yet ...
        if sk == customer_sk and state["stale"]:
            state["stale"] = False
            apply(repo, first["importId"], key="apply-key-first")  # ... and job 1 creates it meanwhile.
            return None
        return real_get(business_id, sk)

    with patch.object(repo, "_get", side_effect=stale_get):
        result = apply(repo, second["importId"], key="apply-key-second")

    assert result["status"] == "applied"
    assert len(customers(repo)) == 1 and len(charges(repo)) == 2
    assert result["apply"]["customersReused"] == 1 and result["apply"]["customersCreated"] == 0


@mock_aws
def test_a_staff_charge_taking_the_next_folio_mid_apply_never_produces_a_duplicate_folio():
    repo = repo_with_business()
    staff_customer = repo.create_customer(BUSINESS_ID, "Cliente manual", None, "manual-customer-1")["customerId"]
    job = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-c5", records(3))
    real_get = repo._get
    state = {"raced": False}

    def racing_get(business_id, sk):
        value = real_get(business_id, sk)
        if sk == "FOLIO_COUNTER" and not state["raced"]:
            state["raced"] = True
            repo.create_charge(BUSINESS_ID, staff_customer, 777, "MXN", "Cobro manual", NOW.date(), creation_key="manual-charge-1")
        return value

    with patch.object(repo, "_get", side_effect=racing_get):
        result = apply(repo, job["importId"])

    assert result["status"] == "applied"
    folios = sorted(c["folio"] for c in charges(repo))
    assert folios == [f"CN-{n:06d}" for n in range(1, 5)] and len(set(folios)) == 4


# --- identity and payload conflicts ---------------------------------------------------

@mock_aws
def test_a_repeated_customer_external_id_with_the_same_identity_is_reused_across_jobs():
    repo = repo_with_business()
    one = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-d1", [record(2, "CLI-1", "Uno", "u@example.com", "FAC-1", 100)])
    two = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-d2", [record(2, "CLI-1", "Uno", None, "FAC-2", 200)])  # email omitted: no contradiction
    apply(repo, one["importId"], key="apply-key-one")
    result = apply(repo, two["importId"], key="apply-key-two")
    assert result["status"] == "applied" and result["apply"]["customersReused"] == 1 and result["apply"]["customersCreated"] == 0
    assert len(customers(repo)) == 1 and len(charges(repo)) == 2
    assert customers(repo)[0].get("email") == "u@example.com"  # a reused customer is never modified


@mock_aws
@pytest.mark.parametrize("name,email", [("Nombre Distinto", "u@example.com"), ("Uno", "otro@example.com")])
def test_a_customer_external_id_with_an_incompatible_identity_is_blocked_for_review(name, email):
    repo = repo_with_business()
    one = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-d3", [record(2, "CLI-1", "Uno", "u@example.com", "FAC-1", 100)])
    two = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-d4", [
        record(2, "CLI-1", name, email, "FAC-2", 200),
        record(3, "CLI-9", "Otro Cliente", None, "FAC-3", 300),  # unaffected row still applies
    ])
    apply(repo, one["importId"], key="apply-key-one")
    result = apply(repo, two["importId"], key="apply-key-two")

    assert result["status"] == "review"
    assert result["apply"]["conflicts"] == 1 and result["apply"]["chargesCreated"] == 1 and result["apply"]["rowsProcessed"] == 2
    assert result["apply"]["conflictSample"] == [{"sourceRow": 2, "code": "customer_identity_conflict"}]
    assert {c["external_id"] for c in charges(repo)} == {"FAC-1", "FAC-3"}  # FAC-2 was NOT created
    original = [c for c in customers(repo) if c["external_id"] == "CLI-1"][0]
    assert original["display_name"] == "Uno" and original["email"] == "u@example.com"  # untouched


@mock_aws
def test_an_existing_charge_with_the_same_payload_is_idempotent_across_jobs_and_consumes_no_folio():
    repo = repo_with_business()
    row = record(2, "CLI-1", "Uno", "u@example.com", "FAC-1", 100)
    one = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-e1", [row])
    two = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-e2", [row])
    apply(repo, one["importId"], key="apply-key-one")
    counter_before = repo._get(BUSINESS_ID, "FOLIO_COUNTER")["value"]
    result = apply(repo, two["importId"], key="apply-key-two")
    assert result["status"] == "applied" and result["apply"]["chargesReused"] == 1 and result["apply"]["chargesCreated"] == 0
    assert len(charges(repo)) == 1 and len(customers(repo)) == 1
    assert repo._get(BUSINESS_ID, "FOLIO_COUNTER")["value"] == counter_before


@pytest.mark.parametrize("changed", [
    {"amount_minor": 999}, {"description": "Otro concepto"}, {"due_date": "2026-12-31"},
])
@mock_aws
def test_an_existing_charge_with_a_different_payload_is_blocked_and_left_untouched(changed):
    repo = repo_with_business()
    base = dict(customer_id="CLI-1", name="Uno", email="u@example.com", charge_id="FAC-1", amount_minor=100, description="Material", due_date="2026-09-30")
    one = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-e3", [record(2, **base)])
    two = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-e4", [record(2, **{**base, **changed})])
    apply(repo, one["importId"], key="apply-key-one")
    stored_before = dump(charges(repo))
    result = apply(repo, two["importId"], key="apply-key-two")
    assert result["status"] == "review" and result["apply"]["conflicts"] == 1
    assert result["apply"]["conflictSample"] == [{"sourceRow": 2, "code": "charge_payload_conflict"}]
    assert dump(charges(repo)) == stored_before and len(charges(repo)) == 1


@mock_aws
def test_an_existing_cancelled_charge_is_not_reopened_by_a_later_import():
    repo = repo_with_business()
    row = record(2, "CLI-1", "Uno", "u@example.com", "FAC-1", 100)
    one = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-e5", [row])
    two = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-e6", [row])
    apply(repo, one["importId"], key="apply-key-one")
    repo.cancel_charge(BUSINESS_ID, charges(repo)[0]["id"], "membership-1", "cancel-key-0001", NOW)
    result = apply(repo, two["importId"], key="apply-key-two")
    assert result["status"] == "review" and result["apply"]["conflictSample"] == [{"sourceRow": 2, "code": "charge_cancelled"}]
    assert charges(repo)[0].get("cancelled_at")


@mock_aws
def test_a_manually_created_customer_is_never_matched_by_name_alone():
    repo = repo_with_business()
    repo.create_customer(BUSINESS_ID, "Uno", "u@example.com", "manual-customer-2")
    job = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-e7", [record(2, "CLI-1", "Uno", "u@example.com", "FAC-1", 100)])
    result = apply(repo, job["importId"])
    assert result["status"] == "applied" and result["apply"]["customersCreated"] == 1
    assert len(customers(repo)) == 2  # the import did not guess it was the manual one


@mock_aws
def test_a_job_in_review_is_terminal_and_its_result_is_replayed_for_the_same_key():
    repo = repo_with_business()
    one = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-e8", [record(2, "CLI-1", "Uno", None, "FAC-1", 100)])
    two = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-e9", [record(2, "CLI-1", "Distinto", None, "FAC-2", 100)])
    apply(repo, one["importId"], key="apply-key-one")
    first = apply(repo, two["importId"], key="apply-key-two")
    assert first["status"] == "review"
    assert apply(repo, two["importId"], key="apply-key-two") == first
    with pytest.raises(ImportOperationRefused):
        apply(repo, two["importId"], key="apply-key-other")


# --- state refusals -------------------------------------------------------------------

@mock_aws
def test_apply_of_an_invalid_batch_creates_nothing():
    repo = repo_with_business()
    job = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-f1", [record(amount_minor=-5)])
    assert job["status"] == "invalid"
    before = snapshot(repo)
    with pytest.raises(ImportOperationRefused) as refused:
        apply(repo, job["importId"])
    assert refused.value.code == "import_not_applicable" and refused.value.status == "invalid"
    assert snapshot(repo) == before and not customers(repo) and not charges(repo)


@mock_aws
def test_apply_of_a_purged_batch_creates_nothing():
    repo = repo_with_business()
    job = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-f2", records(3))
    repo.purge_import_job(BUSINESS_ID, job["importId"], SUBJECT, "purge-key-0001", NOW)
    before = snapshot(repo)
    with pytest.raises(ImportOperationRefused) as refused:
        apply(repo, job["importId"])
    assert refused.value.status == "purged"
    assert snapshot(repo) == before and not customers(repo)


@mock_aws
def test_apply_of_a_job_being_purged_creates_nothing():
    repo = repo_with_business()
    job = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-f3", records(3))
    metadata = [i for i in business_items(repo) if i["SK"].startswith("IMPORT#")][0]
    repo.table.update_item(Key={"PK": metadata["PK"], "SK": metadata["SK"]}, UpdateExpression="SET #s = :p",
                           ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":p": "purging"})
    with pytest.raises(ImportOperationRefused) as refused:
        apply(repo, job["importId"])
    assert refused.value.status == "purging" and not customers(repo)


@mock_aws
def test_apply_of_an_unknown_import_is_none():
    repo = repo_with_business()
    assert apply(repo, "does-not-exist") is None


@mock_aws
def test_an_interrupted_apply_can_be_purged_and_then_can_no_longer_be_applied():
    repo = repo_with_business()
    job = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-f4", records(10))
    partial = apply(repo, job["importId"], max_rows=4)
    assert partial["status"] == "applying" and partial["apply"]["leaseActive"] is False
    purged = repo.purge_import_job(BUSINESS_ID, job["importId"], SUBJECT, "purge-key-0002", NOW)
    assert purged["status"] == "purged" and purged["purge"]["priorStatus"] == "applying" and purged["purge"]["rowResultsDeleted"] == 4
    assert len(charges(repo)) == 4  # what was already created stays: those are real records
    assert not by_prefix(repo, "IMPORT_APPLYROW#") and not by_prefix(repo, "IMPORT_ROWS#")
    with pytest.raises(ImportOperationRefused):
        apply(repo, job["importId"], max_rows=4)
    assert len(charges(repo)) == 4


# --- server-side revalidation of stored data ------------------------------------------------

@mock_aws
def test_apply_refuses_when_the_temporary_rows_have_expired_and_creates_nothing():
    repo = repo_with_business()
    job = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-g1", records(30), retention_days=1)
    before = snapshot(repo)
    with pytest.raises(ImportOperationRefused) as refused:
        apply(repo, job["importId"], clock=lambda: NOW + timedelta(days=2))  # chunk still physically present
    assert refused.value.code == "import_rows_unavailable"
    assert snapshot(repo) == before and repo.import_job_detail(BUSINESS_ID, job["importId"], NOW)["status"] == "validated"


@mock_aws
def test_apply_refuses_when_a_chunk_is_missing():
    repo = repo_with_business()
    job = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-g2", records(60))
    chunk = by_prefix(repo, f"IMPORT_ROWS#{job['importId']}#")[1]
    repo.table.delete_item(Key={"PK": chunk["PK"], "SK": chunk["SK"]})
    with pytest.raises(ImportOperationRefused) as refused:
        apply(repo, job["importId"])
    assert refused.value.code == "import_rows_unavailable" and not customers(repo)


def _tamper(repo, import_id, mutate):
    chunk = by_prefix(repo, f"IMPORT_ROWS#{import_id}#")[0]
    rows = chunk["rows"]
    mutate(rows)
    repo.table.put_item(Item={**chunk, "rows": rows})


@mock_aws
@pytest.mark.parametrize("mutate", [
    lambda rows: rows[0]["normalized"]["charge"].__setitem__("amountMinor", Decimal(0)),
    lambda rows: rows[0]["normalized"]["charge"].__setitem__("amountMinor", Decimal("12.5")),
    lambda rows: rows[0]["normalized"]["customer"].__setitem__("email", "not-an-email"),
    lambda rows: rows[1]["normalized"]["charge"].__setitem__("externalId", rows[0]["normalized"]["charge"]["externalId"]),
    lambda rows: rows[0].__setitem__("valid", False),
])
def test_apply_revalidates_stored_rows_and_refuses_tampered_data_before_writing_anything(mutate):
    repo = repo_with_business()
    job = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-g3", records(4))
    _tamper(repo, job["importId"], mutate)
    with pytest.raises(ImportOperationRefused) as refused:
        apply(repo, job["importId"])
    assert refused.value.code in ("import_data_invalid", "import_rows_unavailable")
    assert not customers(repo) and not charges(repo) and not by_prefix(repo, "IMPORT_APPLYROW#")
    assert repo.import_job_detail(BUSINESS_ID, job["importId"], NOW)["status"] == "validated"  # never entered `applying`


# --- partial failure, resume --------------------------------------------------------------------

@mock_aws
def test_a_transient_failure_midway_leaves_a_recoverable_job_and_a_retry_completes_it_without_duplicates():
    repo = repo_with_business()
    job = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-h1", records(8, customers=3))
    real = repo.client.transact_write_items
    state = {"calls": 0}

    def flaky(**kwargs):
        state["calls"] += 1
        if state["calls"] == 4:  # call 1 = start/lease, 2-3 = rows 1-2, 4 = row 3
            raise ClientError({"Error": {"Code": "InternalServerError", "Message": "boom"}}, "TransactWriteItems")
        return real(**kwargs)

    with patch.object(repo.client, "transact_write_items", side_effect=flaky):
        broken = apply(repo, job["importId"])

    assert broken["status"] == "applying"
    assert broken["apply"]["rowsProcessed"] == 2 and broken["apply"]["failed"] == 1
    assert broken["apply"]["lastErrorCode"] == "dynamodb_error" and broken["apply"]["leaseActive"] is False
    assert len(charges(repo)) == 2

    done = apply(repo, job["importId"])  # same key: resume
    assert done["status"] == "applied" and done["apply"]["failed"] == 0 and done["apply"]["lastErrorCode"] is None
    assert len(charges(repo)) == 8 and len(customers(repo)) == 3
    assert sorted(c["folio"] for c in charges(repo)) == [f"CN-{n:06d}" for n in range(1, 9)]
    assert done["apply"]["chargesCreated"] == 8 and done["apply"]["customersCreated"] == 3


@mock_aws
def test_a_crash_after_the_row_committed_but_before_the_response_never_repeats_the_row():
    repo = repo_with_business()
    job = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-h2", records(3))
    real = repo.client.transact_write_items
    state = {"calls": 0}

    def commit_then_lose_the_response(**kwargs):
        state["calls"] += 1
        response = real(**kwargs)
        if state["calls"] == 2:  # row 1 committed ...
            raise ClientError({"Error": {"Code": "RequestTimeout", "Message": "response lost"}}, "TransactWriteItems")
        return response

    with patch.object(repo.client, "transact_write_items", side_effect=commit_then_lose_the_response):
        apply(repo, job["importId"])
    done = apply(repo, job["importId"])
    assert done["status"] == "applied" and len(charges(repo)) == 3 and len(customers(repo)) == 3
    assert sorted(c["folio"] for c in charges(repo)) == ["CN-000001", "CN-000002", "CN-000003"]


# --- DynamoDB limits ------------------------------------------------------------------------------

@mock_aws
def test_a_full_500_row_apply_never_approaches_dynamodb_transaction_limits_and_never_scans():
    repo = repo_with_business()
    job = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-i1", records(500, customers=120))
    with Spy(repo) as spy, patch.object(repo.table, "scan", side_effect=AssertionError("apply must not Scan")), \
            patch.object(repo.client, "scan", side_effect=AssertionError("apply must not Scan")):
        view, slices = None, 0
        while view is None or view["status"] == "applying":
            # time_budget_s is lifted so the ROW bound is measured deterministically;
            # the time bound has its own test (Moto is slower than real DynamoDB).
            view = apply(repo, job["importId"], max_rows=imports.APPLY_ROWS_PER_INVOCATION, time_budget_s=1e9)
            slices += 1
            assert slices <= 12
    assert view["status"] == "applied" and view["apply"]["rowsProcessed"] == 500
    assert view["apply"]["chargesCreated"] == 500 and view["apply"]["customersCreated"] == 120 and view["apply"]["customersReused"] == 380
    assert len(charges(repo)) == 500 and len(customers(repo)) == 120
    assert max(count for count, _ in spy.calls) <= 6            # one row = fence + result + customer + folio + charge
    assert max(size for _, size in spy.calls) < 64 * 1024        # nowhere near 4 MB
    assert slices == 10                                          # 500 rows / 50 per invocation


# --- HTTP / authorization / isolation ------------------------------------------------------------------

def _http_apply(runtime, key, groups, import_id, business_id=BUSINESS_ID, subject=SUBJECT):
    return handle_platform(platform_event("POST", ROUTE_APPLY, subject, key, None, groups, business_id, import_id), runtime)


@mock_aws
def test_http_apply_is_superadmin_only_and_reveals_nothing_when_denied(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_GROUP", "cauce-super-admin")
    repo = repo_with_business()
    job = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-j1", records(3))
    runtime = RealRuntime(repo)
    before = snapshot(repo)
    for groups in ([], ["staff"], ["cauce-super-admin-x"]):
        response = _http_apply(runtime, "apply-key-0001", groups, job["importId"])
        assert response["statusCode"] == 403 and json.loads(response["body"]) == {"error": "forbidden"}
    assert _http_apply(runtime, "apply-key-0001", ["staff"], "unknown")["statusCode"] == 403
    assert snapshot(repo) == before and not customers(repo)


@mock_aws
def test_http_apply_contract_statuses_bodies_and_idempotent_replay(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_GROUP", "cauce-super-admin")
    repo = repo_with_business()
    job = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-j2", [record(customer_id=PII_EXTERNAL_ID, name=PII_NAME, email=PII_EMAIL)])
    invalid = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-j3", [record(amount_minor=-1)])
    runtime = RealRuntime(repo)

    assert _http_apply(runtime, None, SUPER, job["importId"])["statusCode"] == 400          # Idempotency-Key required
    assert _http_apply(runtime, "short", SUPER, job["importId"])["statusCode"] == 400
    assert _http_apply(runtime, "apply-key-0001", SUPER, "does-not-exist")["statusCode"] == 404
    refused = _http_apply(runtime, "apply-key-0001", SUPER, invalid["importId"])
    assert refused["statusCode"] == 409 and json.loads(refused["body"]) == {"error": "import_not_applicable", "status": "invalid"}
    assert not customers(repo)

    done = _http_apply(runtime, "apply-key-0001", SUPER, job["importId"])
    body = json.loads(done["body"])
    assert done["statusCode"] == 200 and body["status"] == "applied" and body["apply"]["chargesCreated"] == 1
    readable = dump(body)
    for secret in (PII_NAME, PII_EMAIL, PII_EXTERNAL_ID):
        assert secret not in readable
    assert _http_apply(runtime, "apply-key-0001", SUPER, job["importId"]) == done            # same key: same response
    other = _http_apply(runtime, "apply-key-9999", SUPER, job["importId"])
    assert other["statusCode"] == 409 and json.loads(other["body"])["status"] == "applied"
    assert len(charges(repo)) == 1


@mock_aws
def test_apply_never_crosses_the_business_boundary_even_with_identical_external_ids():
    repo = repo_with_business()
    repo.put_tenant_manifest(manifest(OTHER_BUSINESS_ID))
    rows = [record(2, "CLI-1", "Uno", "u@example.com", "FAC-1", 100)]
    a = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-k1", rows)
    b = create_job(repo, OTHER_BUSINESS_ID, SUBJECT, "import-key-k2", rows)

    # Business B cannot apply (or even see) business A's import.
    assert apply(repo, a["importId"], business_id=OTHER_BUSINESS_ID) is None
    assert not customers(repo) and not customers(repo, OTHER_BUSINESS_ID)

    apply(repo, a["importId"])
    apply(repo, b["importId"], business_id=OTHER_BUSINESS_ID)
    assert len(customers(repo)) == 1 and len(customers(repo, OTHER_BUSINESS_ID)) == 1
    assert customers(repo)[0]["id"] != customers(repo, OTHER_BUSINESS_ID)[0]["id"]  # same externalId, distinct identities
    assert all(item["business_id"] == BUSINESS_ID for item in customers(repo) + charges(repo))
    assert all(item["business_id"] == OTHER_BUSINESS_ID for item in customers(repo, OTHER_BUSINESS_ID) + charges(repo, OTHER_BUSINESS_ID))


def test_apply_service_call_is_authorized_before_touching_the_repository():
    class Boom:
        def __getattr__(self, name):
            raise AssertionError("repository must not be reached")

    with pytest.raises(PlatformForbidden):
        PlatformService(Boom(), lambda: NOW).apply_import("business-1", "subject-1", None, "import-1", "apply-key-0001")
    with pytest.raises(ValueError):
        PlatformService(Boom(), lambda: NOW).apply_import("business-1", "subject-1", "super_admin", "import-1", "x")


# --- purge x apply interaction ---------------------------------------------------------------

@mock_aws
def test_purge_during_a_live_apply_slice_is_refused_and_the_apply_still_completes():
    repo = repo_with_business()
    job = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-l1", records(4))
    original = repo._apply_import_row
    state = {"calls": 0, "refused": None}

    def wrapped(*args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 2:
            with pytest.raises(ImportOperationRefused) as refused:
                repo.purge_import_job(BUSINESS_ID, job["importId"], SUBJECT, "purge-key-0003", NOW)
            state["refused"] = refused.value.code
        return original(*args, **kwargs)

    with patch.object(repo, "_apply_import_row", side_effect=wrapped):
        result = apply(repo, job["importId"])

    assert state["refused"] == "import_apply_active"
    assert result["status"] == "applied" and len(charges(repo)) == 4
    assert len(by_prefix(repo, "IMPORT_ROWS#")) == 1  # nothing was deleted from under the apply


@mock_aws
def test_an_apply_whose_lease_expired_and_was_purged_underneath_it_writes_nothing_more():
    repo = repo_with_business()
    job = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-l2", records(6))
    original = repo._apply_import_row
    state = {"calls": 0}

    def wrapped(*args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 2:  # A stalls, its lease lapses, and the operator purges the job
            metadata = [i for i in business_items(repo) if i["SK"].startswith("IMPORT#")][0]
            repo.table.update_item(Key={"PK": metadata["PK"], "SK": metadata["SK"]},
                                   UpdateExpression="SET apply_lease_expires_at = :e", ExpressionAttributeValues={":e": (NOW - timedelta(seconds=1)).isoformat()})
            repo.purge_import_job(BUSINESS_ID, job["importId"], SUBJECT, "purge-key-0004", NOW)
        return original(*args, **kwargs)

    with patch.object(repo, "_apply_import_row", side_effect=wrapped):
        result = apply(repo, job["importId"])

    assert result["status"] == "purged"
    assert len(charges(repo)) == 1  # only the row that committed before the purge
    assert not by_prefix(repo, "IMPORT_APPLYROW#") and not by_prefix(repo, "IMPORT_ROWS#") and not by_prefix(repo, "IMPORT#")


@mock_aws
def test_a_displaced_apply_cannot_add_new_rows_to_a_job_a_purge_has_already_fenced():
    """The fence's real job: rows are idempotent by key, but a stale slice
    writing a NEW row after the purge listed its children would leave an
    orphan. The purge is interrupted right after it fenced the job, exactly
    the window where that could happen."""
    repo = repo_with_business()
    job = create_job(repo, BUSINESS_ID, SUBJECT, "import-key-l3", records(6))
    original = repo._apply_import_row
    state = {"calls": 0}

    def wrapped(*args, **kwargs):
        state["calls"] += 1
        if state["calls"] == 2:
            metadata = [i for i in business_items(repo) if i["SK"].startswith("IMPORT#")][0]
            repo.table.update_item(Key={"PK": metadata["PK"], "SK": metadata["SK"]},
                                   UpdateExpression="SET apply_lease_expires_at = :e", ExpressionAttributeValues={":e": (NOW - timedelta(seconds=1)).isoformat()})
            with patch.object(repo.client, "transact_write_items", side_effect=ClientError({"Error": {"Code": "InternalServerError", "Message": "boom"}}, "TransactWriteItems")):
                with pytest.raises(ClientError):
                    repo.purge_import_job(BUSINESS_ID, job["importId"], SUBJECT, "purge-key-0005", NOW)  # fenced, then crashed
        return original(*args, **kwargs)

    with patch.object(repo, "_apply_import_row", side_effect=wrapped):
        result = apply(repo, job["importId"])

    assert result["status"] == "purging"
    assert len(by_prefix(repo, "IMPORT_APPLYROW#")) == 1 and len(charges(repo)) == 1  # row 2 was never written
    resumed = repo.purge_import_job(BUSINESS_ID, job["importId"], SUBJECT, "purge-key-0005", NOW)
    assert resumed["status"] == "purged" and not by_prefix(repo, "IMPORT_APPLYROW#")
