"""DynamoDB/Moto integration coverage for platform import jobs.

No AWS account or local daemon needed -- same style as
test_dynamodb_payment_flow.py: a real DynamoRepository against a real (but
in-memory, Moto-backed) DynamoDB table, never a fake.
"""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
import hashlib
import json
import os
import sys

import boto3
from moto import mock_aws
import pytest
from botocore.exceptions import ClientError

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from payments.dynamodb import DynamoRepository, _iso
from payments import imports
from payments.imports import ImportIdempotencyConflict
from payments.platform import PlatformService
from payments.platform_transport import handle_platform


NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)
BUSINESS_ID = "11111111-1111-4111-8111-111111111111"
OTHER_BUSINESS_ID = "22222222-2222-4222-8222-222222222222"


def table():
    resource = boto3.resource("dynamodb", region_name="us-east-1")
    resource.create_table(
        TableName="payments", BillingMode="PAY_PER_REQUEST",
        AttributeDefinitions=[{"AttributeName": key, "AttributeType": "S"} for key in ("PK", "SK", "GSI1PK", "GSI1SK", "GSI2PK", "GSI2SK", "GSI3PK", "GSI3SK")],
        KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
        GlobalSecondaryIndexes=[
            {"IndexName": "LookupIndex", "KeySchema": [{"AttributeName": "GSI1PK", "KeyType": "HASH"}, {"AttributeName": "GSI1SK", "KeyType": "RANGE"}], "Projection": {"ProjectionType": "ALL"}},
            {"IndexName": "SubjectIndex", "KeySchema": [{"AttributeName": "GSI2PK", "KeyType": "HASH"}, {"AttributeName": "GSI2SK", "KeyType": "RANGE"}], "Projection": {"ProjectionType": "ALL"}},
            {"IndexName": "WorkIndex", "KeySchema": [{"AttributeName": "GSI3PK", "KeyType": "HASH"}, {"AttributeName": "GSI3SK", "KeyType": "RANGE"}], "Projection": {"ProjectionType": "ALL"}},
        ],
    )
    return resource


def manifest(business_id):
    return {
        "business": {"id": business_id, "displayName": "Cobranza Norte", "folioPrefix": "CN", "branding": {}},
        "memberships": [],
        "mercadoPago": {"id": f"connection-{business_id[:4]}", "providerAccountId": "seller-1", "credentialSecretRef": "arn:credentials", "webhookSecretRef": "arn:webhook"},
    }


def _record(source_row=2, charge_id="FAC-1", amount_minor=1250000):
    return {
        "sourceRow": source_row,
        "customer": {"externalId": "CLI-1", "displayName": "Ferretería del Norte", "email": "cobros@example.com"},
        "charge": {"externalId": charge_id, "amountMinor": amount_minor, "currency": "MXN", "description": "Material", "dueDate": "2026-09-30"},
    }


def _source():
    return {"fileName": "cartera.csv", "format": "csv"}


def _profile():
    return {"name": "Cobranza estándar MX", "delimiter": "comma", "dateFormat": "iso", "decimalSeparator": "dot", "currency": "MXN"}


def _create(repo, business_id, subject_id, key, records, now=NOW):
    """Run the same source/profile/records through the real domain module
    the platform service uses, then persist -- exercising the exact path
    production takes, not a hand-rolled shortcut."""
    validation = imports.validate_batch(records)
    digest = imports.canonical_digest(_source(), _profile(), records)
    chunks = imports.chunk_rows(validation["rowResults"])
    return repo.create_import_job(
        business_id, subject_id, key, digest, validation["status"],
        _source(), _profile(), validation["summary"], validation["issues"], validation["issuesTruncated"],
        chunks, now, rows_expires_at=now + timedelta(days=30),
    )


@mock_aws
def test_a_clean_batch_creates_a_validated_job():
    repo = DynamoRepository("payments", resource=table())
    repo.put_tenant_manifest(manifest(BUSINESS_ID))
    job = _create(repo, BUSINESS_ID, "subject-1", "import-key-clean-1", [_record()])
    assert job["status"] == "validated"
    assert job["summary"] == {"inputRows": 1, "validRows": 1, "errorRows": 0, "totalMinor": 1250000}
    assert job["businessId"] == BUSINESS_ID


@mock_aws
def test_a_batch_with_errors_creates_an_invalid_job_and_writes_no_customer_or_charge():
    repo = DynamoRepository("payments", resource=table())
    repo.put_tenant_manifest(manifest(BUSINESS_ID))
    job = _create(repo, BUSINESS_ID, "subject-1", "import-key-invalid-1", [_record(amount_minor=-1)])
    assert job["status"] == "invalid"
    # Nothing that looks like a financial write exists for this business --
    # this pass only ever produces import_job/import_key/import_lookup/
    # import_rows/audit entities.
    assert repo._items(BUSINESS_ID, "CUSTOMER#") == []
    assert repo._items(BUSINESS_ID, "CHARGE#") == []
    assert repo.list_customers(BUSINESS_ID) == []
    assert repo.list_charges(BUSINESS_ID) == []


@mock_aws
def test_same_key_and_same_payload_returns_the_same_import_id():
    repo = DynamoRepository("payments", resource=table())
    repo.put_tenant_manifest(manifest(BUSINESS_ID))
    first = _create(repo, BUSINESS_ID, "subject-1", "import-key-idem-1", [_record()])
    second = _create(repo, BUSINESS_ID, "subject-1", "import-key-idem-1", [_record()])
    assert first["importId"] == second["importId"]
    assert first == second


@mock_aws
def test_same_key_with_a_different_payload_is_a_conflict():
    repo = DynamoRepository("payments", resource=table())
    repo.put_tenant_manifest(manifest(BUSINESS_ID))
    _create(repo, BUSINESS_ID, "subject-1", "import-key-conflict-1", [_record()])
    with pytest.raises(ImportIdempotencyConflict):
        _create(repo, BUSINESS_ID, "subject-1", "import-key-conflict-1", [_record(amount_minor=999)])


@mock_aws
def test_immediate_read_after_create_is_strongly_consistent_not_eventual():
    """Never routes through a GSI for the by-id path -- see
    create_import_job's own docstring for why. This proves the read that
    follows a write moments later never misses."""
    repo = DynamoRepository("payments", resource=table())
    repo.put_tenant_manifest(manifest(BUSINESS_ID))
    job = _create(repo, BUSINESS_ID, "subject-1", "import-key-consistency-1", [_record()])
    detail = repo.import_job_detail(BUSINESS_ID, job["importId"])
    assert detail == job


@mock_aws
def test_no_scan_is_ever_used_for_create_read_or_list():
    repo = DynamoRepository("payments", resource=table())
    repo.put_tenant_manifest(manifest(BUSINESS_ID))
    with patch.object(repo.table, "scan", side_effect=AssertionError("Scan must never be used for import jobs")):
        job = _create(repo, BUSINESS_ID, "subject-1", "import-key-noscan-1", [_record()])
        repo.import_job_detail(BUSINESS_ID, job["importId"])
        repo.list_import_jobs(BUSINESS_ID, 50, None)


@mock_aws
def test_business_isolation_a_second_business_never_sees_the_firsts_imports():
    repo = DynamoRepository("payments", resource=table())
    repo.put_tenant_manifest(manifest(BUSINESS_ID))
    repo.put_tenant_manifest(manifest(OTHER_BUSINESS_ID))
    job = _create(repo, BUSINESS_ID, "subject-1", "import-key-isolation-1", [_record()])
    _create(repo, OTHER_BUSINESS_ID, "subject-1", "import-key-isolation-2", [_record(charge_id="FAC-OTHER")])

    assert repo.import_job_detail(OTHER_BUSINESS_ID, job["importId"]) is None
    listing = repo.list_import_jobs(BUSINESS_ID, 50, None)
    assert len(listing["items"]) == 1
    assert listing["items"][0]["importId"] == job["importId"]
    other_listing = repo.list_import_jobs(OTHER_BUSINESS_ID, 50, None)
    assert all(item["importId"] != job["importId"] for item in other_listing["items"])


@mock_aws
def test_listing_uses_query_and_paginates_with_a_real_cursor():
    repo = DynamoRepository("payments", resource=table())
    repo.put_tenant_manifest(manifest(BUSINESS_ID))
    created = []
    for index in range(5):
        job = _create(repo, BUSINESS_ID, "subject-1", f"import-key-page-{index}", [_record(charge_id=f"FAC-PAGE-{index}")])
        created.append(job["importId"])

    first_page = repo.list_import_jobs(BUSINESS_ID, 2, None)
    assert len(first_page["items"]) == 2
    assert first_page["hasMore"] is True
    assert first_page["cursor"]

    second_page = repo.list_import_jobs(BUSINESS_ID, 2, first_page["cursor"])
    assert len(second_page["items"]) == 2
    assert second_page["hasMore"] is True

    third_page = repo.list_import_jobs(BUSINESS_ID, 2, second_page["cursor"])
    assert len(third_page["items"]) == 1
    assert third_page["hasMore"] is False
    assert third_page["cursor"] is None

    all_ids = {item["importId"] for page in (first_page, second_page, third_page) for item in page["items"]}
    assert all_ids == set(created)


@mock_aws
def test_a_forged_cursor_for_another_business_is_rejected_not_followed():
    repo = DynamoRepository("payments", resource=table())
    repo.put_tenant_manifest(manifest(BUSINESS_ID))
    repo.put_tenant_manifest(manifest(OTHER_BUSINESS_ID))
    _create(repo, BUSINESS_ID, "subject-1", "import-key-forge-1", [_record()])
    job_other = _create(repo, OTHER_BUSINESS_ID, "subject-1", "import-key-forge-2", [_record(charge_id="FAC-OTHER")])
    # Build a cursor that legitimately belongs to OTHER_BUSINESS_ID's partition...
    from payments.dynamodb import _encode_import_cursor
    forged = _encode_import_cursor({"PK": f"BUSINESS#{OTHER_BUSINESS_ID}", "SK": f"IMPORT#{_iso(NOW)}#{job_other['importId']}"})
    # ...and try to use it while listing BUSINESS_ID's own imports.
    with pytest.raises(ValueError):
        repo.list_import_jobs(BUSINESS_ID, 50, forged)


@mock_aws
def test_rows_and_chunks_never_bleed_between_two_imports_in_the_same_business():
    repo = DynamoRepository("payments", resource=table())
    repo.put_tenant_manifest(manifest(BUSINESS_ID))
    records_a = [_record(source_row=n, charge_id=f"FAC-A-{n}") for n in range(2, 2 + imports.ROWS_PER_CHUNK + 5)]
    records_b = [_record(source_row=n, charge_id=f"FAC-B-{n}") for n in range(2, 2 + imports.ROWS_PER_CHUNK + 3)]
    job_a = _create(repo, BUSINESS_ID, "subject-1", "import-key-chunks-a", records_a)
    job_b = _create(repo, BUSINESS_ID, "subject-1", "import-key-chunks-b", records_b)
    assert job_a["importId"] != job_b["importId"]

    chunks_a = [item for item in repo._items(BUSINESS_ID, f"IMPORT_ROWS#{job_a['importId']}#")]
    chunks_b = [item for item in repo._items(BUSINESS_ID, f"IMPORT_ROWS#{job_b['importId']}#")]
    assert len(chunks_a) == 2  # 30 rows / 25 per chunk
    assert len(chunks_b) == 2  # 28 rows / 25 per chunk
    for chunk in chunks_a:
        assert chunk["import_id"] == job_a["importId"]
        assert all(row["sourceRow"] < 100 for row in chunk["rows"])
    for chunk in chunks_b:
        assert chunk["import_id"] == job_b["importId"]
    # No chunk of A is reachable under B's prefix and vice versa.
    assert repo._items(BUSINESS_ID, f"IMPORT_ROWS#{job_a['importId']}#") != []
    assert not any(item["import_id"] == job_a["importId"] for item in chunks_b)


@mock_aws
def test_max_batch_never_exceeds_the_dynamodb_transactional_item_limit():
    repo = DynamoRepository("payments", resource=table())
    repo.put_tenant_manifest(manifest(BUSINESS_ID))
    records = [_record(source_row=n, charge_id=f"FAC-MAX-{n}") for n in range(2, 2 + imports.MAX_RECORDS_PER_IMPORT)]
    job = _create(repo, BUSINESS_ID, "subject-1", "import-key-max-1", records)
    assert job["summary"]["inputRows"] == imports.MAX_RECORDS_PER_IMPORT
    chunk_count = len(repo._items(BUSINESS_ID, f"IMPORT_ROWS#{job['importId']}#"))
    # guard + metadata + lookup + audit + chunks must stay far under
    # DynamoDB's 100-item TransactWriteItems ceiling.
    assert 4 + chunk_count < 100


@mock_aws
def test_a_transaction_conflict_on_one_item_never_leaves_another_item_behind(monkeypatch):
    """Atomicity proof: pre-collide exactly the metadata item a fixed
    import_id will try to Put, and confirm the OTHER item in that same
    transaction (the idempotency guard) is never left behind either --
    DynamoDB's TransactWriteItems is all-or-nothing, and this asserts on
    that instead of just trusting it."""
    repo = DynamoRepository("payments", resource=table())
    repo.put_tenant_manifest(manifest(BUSINESS_ID))
    fixed_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    monkeypatch.setattr("payments.dynamodb._uuid", lambda: fixed_id)
    monkeypatch.setattr("payments.dynamodb._backoff_sleep", lambda attempt: None)
    collided_sk = f"IMPORT#{_iso(NOW)}#{fixed_id}"
    repo.table.put_item(Item={"PK": f"BUSINESS#{BUSINESS_ID}", "SK": collided_sk, "entity": "pre-existing-collision"})

    with pytest.raises(RuntimeError, match="repeatedly"):
        _create(repo, BUSINESS_ID, "subject-1", "import-key-orphan-1", [_record()])

    guard_key = hashlib.sha256(f"subject-1\nimport-key-orphan-1".encode()).hexdigest()
    assert repo._get(BUSINESS_ID, f"IMPORT_KEY#{guard_key}") is None
    assert repo._get(BUSINESS_ID, f"IMPORT_LOOKUP#{fixed_id}") is None
    # The pre-seeded collision item is the only thing at that SK -- proving
    # the real metadata Put never landed either.
    assert repo._get(BUSINESS_ID, collided_sk) == {"PK": f"BUSINESS#{BUSINESS_ID}", "SK": collided_sk, "entity": "pre-existing-collision"}


# --- Hallazgo 1: audit isolation between different superadmins ------------

@mock_aws
def test_two_subjects_sharing_the_same_business_and_idempotency_key_create_two_distinct_jobs():
    repo = DynamoRepository("payments", resource=table())
    repo.put_tenant_manifest(manifest(BUSINESS_ID))
    job_a = _create(repo, BUSINESS_ID, "subject-A", "shared-import-key", [_record()])
    job_b = _create(repo, BUSINESS_ID, "subject-B", "shared-import-key", [_record()])
    assert job_a["importId"] != job_b["importId"]
    listing = repo.list_import_jobs(BUSINESS_ID, 50, None)
    assert len(listing["items"]) == 2
    audits = repo._items(BUSINESS_ID, "AUDIT#import")
    assert len(audits) == 2
    assert {audit["actor_id"] for audit in audits} == {"subject-A", "subject-B"}


@mock_aws
def test_retrying_the_same_subject_and_key_returns_the_same_job_and_does_not_add_a_second_audit():
    repo = DynamoRepository("payments", resource=table())
    repo.put_tenant_manifest(manifest(BUSINESS_ID))
    _create(repo, BUSINESS_ID, "subject-A", "shared-import-key", [_record()])
    _create(repo, BUSINESS_ID, "subject-B", "shared-import-key", [_record()])
    retry_a = _create(repo, BUSINESS_ID, "subject-A", "shared-import-key", [_record()])
    listing = repo.list_import_jobs(BUSINESS_ID, 50, None)
    assert len(listing["items"]) == 2  # still exactly two jobs, not three
    audits = repo._items(BUSINESS_ID, "AUDIT#import")
    assert len(audits) == 2  # still exactly two audits
    assert retry_a["importId"] in {item["importId"] for item in listing["items"]}


# --- Hallazgo 2: sourceRow/amount sanitization before persistence ---------

@pytest.mark.parametrize("bad_source_row", [2.5, True, "2", [2], {"n": 2}, None])
@mock_aws
def test_a_structurally_invalid_source_row_still_persists_an_invalid_job_without_a_boto3_error(bad_source_row):
    """Documented policy (see payments/imports.py and the delivery doc): a
    single row with a malformed sourceRow marks that row -- and therefore
    the whole batch -- invalid, but never rejects the request structurally
    (400). It must persist cleanly, with no raw, non-DynamoDB-safe value
    (float, bool, list, dict, arbitrary string) ever reaching storage."""
    repo = DynamoRepository("payments", resource=table())
    repo.put_tenant_manifest(manifest(BUSINESS_ID))
    record = {"sourceRow": bad_source_row, "customer": {"externalId": "CLI-1", "displayName": "X", "email": None}, "charge": {"externalId": "FAC-1", "amountMinor": 100, "currency": "MXN", "description": "x", "dueDate": "2026-09-30"}}
    job = _create(repo, BUSINESS_ID, "subject-1", f"import-key-badrow-{repr(bad_source_row)}", [record])
    assert job["status"] == "invalid"
    assert repo._items(BUSINESS_ID, "CUSTOMER#") == []
    assert repo._items(BUSINESS_ID, "CHARGE#") == []


@mock_aws
def test_amount_minor_float_with_a_valid_source_row_persists_an_invalid_job():
    repo = DynamoRepository("payments", resource=table())
    repo.put_tenant_manifest(manifest(BUSINESS_ID))
    record = {"sourceRow": 2, "customer": {"externalId": "CLI-1", "displayName": "X", "email": None}, "charge": {"externalId": "FAC-1", "amountMinor": 12.5, "currency": "MXN", "description": "x", "dueDate": "2026-09-30"}}
    job = _create(repo, BUSINESS_ID, "subject-1", "import-key-floatamount-1", [record])
    assert job["status"] == "invalid"
    assert repo._items(BUSINESS_ID, "CUSTOMER#") == []
    assert repo._items(BUSINESS_ID, "CHARGE#") == []


@mock_aws
def test_source_row_above_the_pilot_maximum_is_invalid_not_a_float_bypass():
    repo = DynamoRepository("payments", resource=table())
    repo.put_tenant_manifest(manifest(BUSINESS_ID))
    record = _record(source_row=imports.MAX_SOURCE_ROW + 1)
    job = _create(repo, BUSINESS_ID, "subject-1", "import-key-hugerow-1", [record])
    assert job["status"] == "invalid"


# --- Hallazgo 4: idempotency conflict is a real HTTP 409, over real Moto --

class _RealRuntime:
    """No fake repository anywhere in this section -- PlatformService here
    is backed by the same real, Moto-mocked DynamoRepository every other
    test in this file uses, per the explicit instruction not to let a fake
    hide the 409 path from DynamoDB's own real conflict detection."""
    def __init__(self, repo):
        self._service = PlatformService(repo, lambda: NOW)

    def platform_service(self):
        return self._service


def _platform_event(method, route, subject, key, body, groups, business_id=BUSINESS_ID, import_id=None):
    return {
        "requestContext": {"http": {"method": method}, "routeKey": route, "authorizer": {"jwt": {"claims": {"sub": subject, "cognito:groups": groups}}}},
        "pathParameters": {"businessId": business_id, **({"importId": import_id} if import_id else {})},
        "queryStringParameters": {},
        "headers": {"Idempotency-Key": key},
        "body": json.dumps(body),
    }


def _validate_body(records):
    return {"source": _source(), "profile": _profile(), "records": records}


@mock_aws
def test_http_same_key_and_same_payload_returns_200_and_the_same_import_id(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_GROUP", "cauce-super-admin")
    repo = DynamoRepository("payments", resource=table())
    repo.put_tenant_manifest(manifest(BUSINESS_ID))
    runtime = _RealRuntime(repo)
    body = _validate_body([_record()])
    first = handle_platform(_platform_event("POST", "POST /platform/businesses/{businessId}/imports/validate", "subject-1", "import-key-http-1", body, ["cauce-super-admin"]), runtime)
    second = handle_platform(_platform_event("POST", "POST /platform/businesses/{businessId}/imports/validate", "subject-1", "import-key-http-1", body, ["cauce-super-admin"]), runtime)
    assert first["statusCode"] == 200
    assert second["statusCode"] == 200
    assert json.loads(first["body"])["importId"] == json.loads(second["body"])["importId"]


@mock_aws
def test_http_same_key_and_different_payload_is_409_not_400(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_GROUP", "cauce-super-admin")
    repo = DynamoRepository("payments", resource=table())
    repo.put_tenant_manifest(manifest(BUSINESS_ID))
    runtime = _RealRuntime(repo)
    first = handle_platform(_platform_event("POST", "POST /platform/businesses/{businessId}/imports/validate", "subject-1", "import-key-http-2", _validate_body([_record()]), ["cauce-super-admin"]), runtime)
    second = handle_platform(_platform_event("POST", "POST /platform/businesses/{businessId}/imports/validate", "subject-1", "import-key-http-2", _validate_body([_record(amount_minor=999)]), ["cauce-super-admin"]), runtime)
    assert first["statusCode"] == 200
    assert second["statusCode"] == 409
    assert json.loads(second["body"]) == {"error": "operation_conflict"}


@mock_aws
def test_http_structurally_invalid_payload_is_still_400(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_GROUP", "cauce-super-admin")
    repo = DynamoRepository("payments", resource=table())
    repo.put_tenant_manifest(manifest(BUSINESS_ID))
    runtime = _RealRuntime(repo)
    body = {"source": _source(), "profile": _profile(), "records": []}  # empty batch: structural, not idempotency
    response = handle_platform(_platform_event("POST", "POST /platform/businesses/{businessId}/imports/validate", "subject-1", "import-key-http-3", body, ["cauce-super-admin"]), runtime)
    assert response["statusCode"] == 400
    assert json.loads(response["body"]) == {"error": "invalid_request"}


@mock_aws
def test_http_nonexistent_business_keeps_its_documented_policy(monkeypatch):
    """create_import_job's own documented policy: a nonexistent business is
    a 400 (invalid_request), the same bucket every other structural problem
    in this request falls into -- not confused with a 409 idempotency
    conflict, and not silently accepted."""
    monkeypatch.setenv("PLATFORM_ADMIN_GROUP", "cauce-super-admin")
    repo = DynamoRepository("payments", resource=table())
    runtime = _RealRuntime(repo)
    response = handle_platform(_platform_event("POST", "POST /platform/businesses/{businessId}/imports/validate", "subject-1", "import-key-http-4", _validate_body([_record()]), ["cauce-super-admin"], business_id="does-not-exist"), runtime)
    assert response["statusCode"] == 400


# --- Hallazgo 5: audit action matches the real job status ------------------

@mock_aws
def test_audit_action_is_import_validated_for_a_clean_batch():
    repo = DynamoRepository("payments", resource=table())
    repo.put_tenant_manifest(manifest(BUSINESS_ID))
    job = _create(repo, BUSINESS_ID, "subject-1", "import-key-audit-clean-1", [_record()])
    audits = repo._items(BUSINESS_ID, "AUDIT#import")
    assert len(audits) == 1
    assert audits[0]["action"] == "import.validated"
    assert audits[0]["aggregate_id"] == job["importId"]


@mock_aws
def test_audit_action_is_import_invalid_for_a_batch_with_errors_never_import_validated():
    repo = DynamoRepository("payments", resource=table())
    repo.put_tenant_manifest(manifest(BUSINESS_ID))
    job = _create(repo, BUSINESS_ID, "subject-1", "import-key-audit-invalid-1", [_record(amount_minor=-1)])
    assert job["status"] == "invalid"
    audits = repo._items(BUSINESS_ID, "AUDIT#import")
    assert len(audits) == 1
    assert audits[0]["action"] == "import.invalid"
    assert audits[0]["aggregate_id"] == job["importId"]
    assert not any(audit["action"] == "import.validated" and audit["aggregate_id"] == job["importId"] for audit in audits)


# --- Hallazgo 8: ambiguous customer.externalId within a batch --------------

def _record_for(source_row, external_id, name, email, charge_id):
    return {
        "sourceRow": source_row,
        "customer": {"externalId": external_id, "displayName": name, "email": email},
        "charge": {"externalId": charge_id, "amountMinor": 1000, "currency": "MXN", "description": "x", "dueDate": "2026-09-30"},
    }


@mock_aws
def test_same_customer_id_same_identity_two_charges_is_valid():
    repo = DynamoRepository("payments", resource=table())
    repo.put_tenant_manifest(manifest(BUSINESS_ID))
    records = [
        _record_for(2, "CLI-1", "Empresa Uno", "a@example.com", "FAC-1"),
        _record_for(3, "CLI-1", "Empresa Uno", "a@example.com", "FAC-2"),
    ]
    job = _create(repo, BUSINESS_ID, "subject-1", "import-key-identity-ok-1", records)
    assert job["status"] == "validated"
    assert job["summary"]["validRows"] == 2


@mock_aws
def test_same_customer_id_different_name_is_invalid():
    repo = DynamoRepository("payments", resource=table())
    repo.put_tenant_manifest(manifest(BUSINESS_ID))
    records = [
        _record_for(2, "CLI-1", "Empresa Uno", "a@example.com", "FAC-1"),
        _record_for(3, "CLI-1", "Otra Empresa", "a@example.com", "FAC-2"),
    ]
    job = _create(repo, BUSINESS_ID, "subject-1", "import-key-identity-bad-name-1", records)
    assert job["status"] == "invalid"
    assert any(issue["field"] == "customerExternalId" and "fila 2" in issue["message"] for issue in job["issues"])


@mock_aws
def test_same_customer_id_different_email_is_invalid():
    repo = DynamoRepository("payments", resource=table())
    repo.put_tenant_manifest(manifest(BUSINESS_ID))
    records = [
        _record_for(2, "CLI-1", "Empresa Uno", "a@example.com", "FAC-1"),
        _record_for(3, "CLI-1", "Empresa Uno", "b@example.com", "FAC-2"),
    ]
    job = _create(repo, BUSINESS_ID, "subject-1", "import-key-identity-bad-email-1", records)
    assert job["status"] == "invalid"
    assert any(issue["field"] == "customerExternalId" for issue in job["issues"])


@mock_aws
def test_email_whitespace_and_case_normalization_never_causes_a_false_conflict():
    repo = DynamoRepository("payments", resource=table())
    repo.put_tenant_manifest(manifest(BUSINESS_ID))
    records = [
        _record_for(2, "CLI-1", "Empresa Uno", "  A@Example.com ", "FAC-1"),
        _record_for(3, "CLI-1", "  Empresa   Uno ", "a@example.com", "FAC-2"),
    ]
    job = _create(repo, BUSINESS_ID, "subject-1", "import-key-identity-normalize-1", records)
    assert job["status"] == "validated"


@mock_aws
def test_a_missing_email_never_conflicts_with_an_earlier_provided_one():
    repo = DynamoRepository("payments", resource=table())
    repo.put_tenant_manifest(manifest(BUSINESS_ID))
    records = [
        _record_for(2, "CLI-1", "Empresa Uno", "a@example.com", "FAC-1"),
        _record_for(3, "CLI-1", "Empresa Uno", None, "FAC-2"),
    ]
    job = _create(repo, BUSINESS_ID, "subject-1", "import-key-identity-missing-email-1", records)
    assert job["status"] == "validated"
