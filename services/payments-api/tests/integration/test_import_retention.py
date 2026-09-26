"""TTL and controlled purge of temporary ETL row data (Moto, real repository)."""

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch
import json

from botocore.exceptions import ClientError
from moto import mock_aws
import pytest

from import_support import (
    BUSINESS_ID, NOW, OTHER_BUSINESS_ID, PII_EMAIL, PII_EXTERNAL_ID, PII_NAME, ROUTE_GET, ROUTE_PURGE, ROUTE_VALIDATE, SUPER,
    RealRuntime, all_items, create_job, dump, manifest, platform_event, record, records, table, validate_body,
)
from payments.dynamodb import DynamoRepository
from payments.imports import ImportOperationRefused
from payments.platform import PlatformForbidden, PlatformService
from payments.platform_transport import handle_platform
from payments.runtime import Runtime


def repo_with_business(business_id=BUSINESS_ID):
    repo = DynamoRepository("payments", resource=table())
    repo.put_tenant_manifest(manifest(business_id))
    return repo


def pii_rows():
    return [record(customer_id=PII_EXTERNAL_ID, name=PII_NAME, email=PII_EMAIL)]


def business_items(repo, business_id=BUSINESS_ID):
    return [item for item in all_items(repo) if item["PK"] == f"BUSINESS#{business_id}"]


def by_prefix(repo, prefix, business_id=BUSINESS_ID):
    return [item for item in business_items(repo, business_id) if item["SK"].startswith(prefix)]


class SpyTransactions:
    """Records every TransactWriteItems call: item count and payload size."""
    def __init__(self, repo):
        self.calls = []
        real = repo.client.transact_write_items

        def spy(**kwargs):
            items = kwargs["TransactItems"]
            self.calls.append((len(items), len(json.dumps(items, default=str).encode())))
            return real(**kwargs)

        self.patch = patch.object(repo.client, "transact_write_items", side_effect=spy)

    def __enter__(self):
        self.patch.start()
        return self

    def __exit__(self, *exc):
        self.patch.stop()


# --- TTL --------------------------------------------------------------------

@mock_aws
def test_row_chunks_carry_a_numeric_ttl_equal_to_the_retention_window():
    repo = repo_with_business()
    job = create_job(repo, BUSINESS_ID, "subject-1", "import-key-ttl-1", records(60), retention_days=30)
    chunks = by_prefix(repo, f"IMPORT_ROWS#{job['importId']}#")
    assert len(chunks) == 3
    expected = int((NOW + timedelta(days=30)).timestamp())
    for chunk in chunks:
        assert isinstance(chunk["ttl"], Decimal)  # DynamoDB TTL requires a Number
        assert int(chunk["ttl"]) == expected


@mock_aws
def test_ttl_is_only_on_temporary_row_chunks_never_on_metadata_guards_lookup_or_audit():
    repo = repo_with_business()
    create_job(repo, BUSINESS_ID, "subject-1", "import-key-ttl-2", records(30))
    with_ttl = [item for item in all_items(repo) if "ttl" in item]
    assert with_ttl, "row chunks must expire"
    assert {item.get("entity") for item in with_ttl} == {"import_rows"}
    untouched = {item.get("entity") for item in all_items(repo) if "ttl" not in item}
    assert {"import_job", "import_key", "import_lookup", "audit"} <= untouched


@mock_aws
def test_metadata_records_when_the_rows_expire_and_the_view_exposes_it():
    repo = repo_with_business()
    job = create_job(repo, BUSINESS_ID, "subject-1", "import-key-ttl-3", records(2), retention_days=7)
    assert job["rowsExpireAt"] == (NOW + timedelta(days=7)).isoformat()
    assert repo.import_job_detail(BUSINESS_ID, job["importId"])["rowsExpireAt"] == job["rowsExpireAt"]


@mock_aws
def test_no_personal_data_outside_the_row_chunks():
    repo = repo_with_business()
    create_job(repo, BUSINESS_ID, "subject-1", "import-key-pii-1", pii_rows())
    outside_chunks = [item for item in all_items(repo) if item.get("entity") != "import_rows"]
    blob = dump(outside_chunks)
    for secret in (PII_NAME, PII_EMAIL, PII_EXTERNAL_ID):
        assert secret not in blob
    assert PII_NAME in dump([item for item in all_items(repo) if item.get("entity") == "import_rows"])


@mock_aws
def test_platform_service_uses_the_configured_retention_and_defaults_to_30_days():
    repo = repo_with_business()
    default_service = PlatformService(repo, lambda: NOW)
    job = default_service.validate_import(BUSINESS_ID, "subject-1", "super_admin", "import-key-cfg-1", validate_body(records(1)))
    assert job["rowsExpireAt"] == (NOW + timedelta(days=30)).isoformat()

    short_service = PlatformService(repo, lambda: NOW, retention_days=3)
    job = short_service.validate_import(BUSINESS_ID, "subject-1", "super_admin", "import-key-cfg-2", validate_body(records(2)))
    assert job["rowsExpireAt"] == (NOW + timedelta(days=3)).isoformat()


@pytest.mark.parametrize("bad", [0, -1, 366, 1.5, True, "30", None])
def test_invalid_retention_configuration_is_refused_not_silently_defaulted(bad):
    with pytest.raises(ValueError):
        PlatformService(object(), lambda: NOW, retention_days=bad)


def test_runtime_reads_the_retention_from_the_environment_and_rejects_garbage():
    def factory(table_name, **kwargs):
        return object()

    def runtime(environ):
        # An inert secret resolver: Runtime would otherwise build a real boto3
        # Secrets Manager client, which needs an AWS region from the ambient
        # environment (present on a developer machine, absent on a CI runner).
        # This test must depend on neither.
        return Runtime(environ=environ, repository_factory=factory, secret_resolver=object())

    assert runtime({"PAYMENTS_TABLE_NAME": "t", "IMPORT_ROWS_RETENTION_DAYS": "45"}).platform_service().retention_days == 45
    assert runtime({"PAYMENTS_TABLE_NAME": "t"}).platform_service().retention_days == 30
    for garbage in ("forever", "0", "366", "-3", "1.5"):
        with pytest.raises(RuntimeError):
            runtime({"PAYMENTS_TABLE_NAME": "t", "IMPORT_ROWS_RETENTION_DAYS": garbage}).platform_service()


@mock_aws
def test_an_expired_but_not_yet_deleted_chunk_is_never_served_as_data():
    repo = repo_with_business()
    job = create_job(repo, BUSINESS_ID, "subject-1", "import-key-expired-1", records(30), retention_days=1)
    later = NOW + timedelta(days=2)  # DynamoDB has not physically deleted it yet
    assert repo.load_import_rows(BUSINESS_ID, job["importId"], later) is None
    assert repo.load_import_rows(BUSINESS_ID, job["importId"], NOW + timedelta(hours=1)) is not None


# --- Purge ------------------------------------------------------------------

@mock_aws
def test_purge_deletes_rows_lookup_metadata_and_guard_and_keeps_only_audit():
    repo = repo_with_business()
    job = create_job(repo, BUSINESS_ID, "subject-1", "import-key-purge-1", pii_rows())
    import_id = job["importId"]

    result = repo.purge_import_job(BUSINESS_ID, import_id, "subject-1", "purge-key-0001", NOW)

    assert result["status"] == "purged" and result["importId"] == import_id and result["businessId"] == BUSINESS_ID
    remaining = business_items(repo)
    assert not [item for item in remaining if item["SK"].startswith(("IMPORT#", "IMPORT_ROWS#", "IMPORT_LOOKUP#", "IMPORT_KEY#", "IMPORT_APPLY"))]
    actions = sorted(item["action"] for item in remaining if item.get("entity") == "audit")
    assert actions == ["import.purged", "import.validated"]


@mock_aws
def test_purge_evidence_has_no_personal_data_and_the_whole_table_is_clean_afterwards():
    repo = repo_with_business()
    job = create_job(repo, BUSINESS_ID, "subject-1", "import-key-purge-2", pii_rows())
    repo.purge_import_job(BUSINESS_ID, job["importId"], "subject-1", "purge-key-0002", NOW)
    blob = dump(all_items(repo))
    for secret in (PII_NAME, PII_EMAIL, PII_EXTERNAL_ID):
        assert secret not in blob
    evidence = [item for item in all_items(repo) if item.get("action") == "import.purged"][0]
    assert evidence["actor_id"] == "subject-1" and evidence["aggregate_id"] == job["importId"]
    assert evidence["details"]["priorStatus"] == "validated"
    assert int(evidence["details"]["chunksDeleted"]) == 1


@mock_aws
def test_purge_is_idempotent_and_changes_nothing_the_second_time():
    repo = repo_with_business()
    job = create_job(repo, BUSINESS_ID, "subject-1", "import-key-purge-3", records(60))
    first = repo.purge_import_job(BUSINESS_ID, job["importId"], "subject-1", "purge-key-0003", NOW)
    snapshot = dump(all_items(repo))
    second = repo.purge_import_job(BUSINESS_ID, job["importId"], "subject-1", "purge-key-0003", NOW + timedelta(hours=1))
    other_key = repo.purge_import_job(BUSINESS_ID, job["importId"], "subject-2", "purge-key-9999", NOW + timedelta(hours=2))
    assert first == second == other_key
    assert dump(all_items(repo)) == snapshot


@mock_aws
def test_a_purged_job_reads_as_purged_not_as_never_existed():
    repo = repo_with_business()
    job = create_job(repo, BUSINESS_ID, "subject-1", "import-key-purge-4", records(2))
    repo.purge_import_job(BUSINESS_ID, job["importId"], "subject-1", "purge-key-0004", NOW)
    detail = repo.import_job_detail(BUSINESS_ID, job["importId"])
    assert detail["status"] == "purged" and detail["importId"] == job["importId"]
    assert "records" not in detail and "rows" not in detail
    assert repo.import_job_detail(BUSINESS_ID, "00000000-0000-4000-8000-000000000000") is None
    assert repo.list_import_jobs(BUSINESS_ID, 50)["items"] == []


@mock_aws
def test_purging_an_unknown_import_is_none_not_an_error():
    repo = repo_with_business()
    assert repo.purge_import_job(BUSINESS_ID, "does-not-exist", "subject-1", "purge-key-0005", NOW) is None


@mock_aws
def test_purge_never_reveals_or_touches_another_business():
    repo = repo_with_business()
    repo.put_tenant_manifest(manifest(OTHER_BUSINESS_ID))
    job = create_job(repo, BUSINESS_ID, "subject-1", "import-key-iso-1", pii_rows())
    before = dump(business_items(repo))

    # Business B asks to purge business A's import id: indistinguishable from "unknown".
    assert repo.purge_import_job(OTHER_BUSINESS_ID, job["importId"], "subject-1", "purge-key-0006", NOW) is None
    assert repo.import_job_detail(OTHER_BUSINESS_ID, job["importId"]) is None
    assert dump(business_items(repo)) == before
    assert not [item for item in business_items(repo, OTHER_BUSINESS_ID) if item.get("action") == "import.purged"]


@mock_aws
def test_purge_of_one_job_leaves_a_sibling_job_of_the_same_business_intact():
    repo = repo_with_business()
    keep = create_job(repo, BUSINESS_ID, "subject-1", "import-key-sib-1", records(30, prefix="KEEP"))
    drop = create_job(repo, BUSINESS_ID, "subject-1", "import-key-sib-2", records(30, prefix="DROP"))
    repo.purge_import_job(BUSINESS_ID, drop["importId"], "subject-1", "purge-key-0007", NOW)
    assert repo.import_job_detail(BUSINESS_ID, keep["importId"])["status"] == "validated"
    assert len(by_prefix(repo, f"IMPORT_ROWS#{keep['importId']}#")) == 2
    assert not by_prefix(repo, f"IMPORT_ROWS#{drop['importId']}#")


@mock_aws
def test_purge_never_uses_a_table_scan():
    repo = repo_with_business()
    job = create_job(repo, BUSINESS_ID, "subject-1", "import-key-scan-1", records(60))
    with patch.object(repo.table, "scan", side_effect=AssertionError("purge must not Scan")), \
         patch.object(repo.client, "scan", side_effect=AssertionError("purge must not Scan")):
        repo.purge_import_job(BUSINESS_ID, job["importId"], "subject-1", "purge-key-0008", NOW)
    assert repo.import_job_detail(BUSINESS_ID, job["importId"])["status"] == "purged"


@mock_aws
def test_purge_of_a_full_500_row_job_with_500_results_stays_under_dynamodb_transaction_limits():
    repo = repo_with_business()
    job = create_job(repo, BUSINESS_ID, "subject-1", "import-key-big-1", records(500))
    import_id = job["importId"]
    # Seed what a finished apply leaves behind: one result item per row.
    for index in range(500):
        repo.table.put_item(Item={"PK": f"BUSINESS#{BUSINESS_ID}", "SK": f"IMPORT_APPLYROW#{import_id}#{index // 25:04d}#{index % 25:02d}",
                                  "entity": "import_row_result", "business_id": BUSINESS_ID, "import_id": import_id, "outcome": "created"})
    with SpyTransactions(repo) as spy:
        repo.purge_import_job(BUSINESS_ID, import_id, "subject-1", "purge-key-0009", NOW)
    assert spy.calls, "purge must delete through transactions"
    assert max(count for count, _ in spy.calls) <= 100
    assert max(size for _, size in spy.calls) < 4 * 1024 * 1024
    assert len(spy.calls) > 1  # 520+ deletes really were split
    assert not [item for item in business_items(repo) if import_id in item["SK"] and item.get("entity") != "audit"]


@mock_aws
def test_purge_interrupted_midway_is_resumable_and_leaves_no_orphans():
    repo = repo_with_business()
    job = create_job(repo, BUSINESS_ID, "subject-1", "import-key-resume-1", records(500))
    import_id = job["importId"]
    real = repo.client.transact_write_items
    state = {"calls": 0}

    def flaky(**kwargs):
        state["calls"] += 1
        if state["calls"] == 2:
            raise ClientError({"Error": {"Code": "InternalServerError", "Message": "boom"}}, "TransactWriteItems")
        return real(**kwargs)

    with patch.object(repo.client, "transact_write_items", side_effect=flaky):
        with pytest.raises(ClientError):
            repo.purge_import_job(BUSINESS_ID, import_id, "subject-1", "purge-key-0010", NOW)

    mid = repo.import_job_detail(BUSINESS_ID, import_id)
    assert mid["status"] == "purging"  # visible and resumable, never half-hidden
    result = repo.purge_import_job(BUSINESS_ID, import_id, "subject-1", "purge-key-0010", NOW)
    assert result["status"] == "purged"
    assert not [item for item in business_items(repo) if import_id in item["SK"] and item.get("entity") != "audit"]
    assert [item["action"] for item in business_items(repo) if item.get("entity") == "audit"].count("import.purged") == 1


def _mark_applying(repo, import_id, lease_expires_at):
    metadata = [item for item in business_items(repo) if item["SK"].startswith("IMPORT#")][0]
    repo.table.update_item(
        Key={"PK": metadata["PK"], "SK": metadata["SK"]},
        UpdateExpression="SET #s = :s, apply_lease_token = :t, apply_lease_expires_at = :e",
        ExpressionAttributeNames={"#s": "status"},
        ExpressionAttributeValues={":s": "applying", ":t": "someone-else", ":e": lease_expires_at.isoformat()},
    )


@mock_aws
def test_purge_is_refused_while_an_apply_holds_a_live_lease_and_deletes_nothing():
    repo = repo_with_business()
    job = create_job(repo, BUSINESS_ID, "subject-1", "import-key-active-1", records(30))
    _mark_applying(repo, job["importId"], NOW + timedelta(seconds=30))
    before = dump(business_items(repo))
    with pytest.raises(ImportOperationRefused) as refused:
        repo.purge_import_job(BUSINESS_ID, job["importId"], "subject-1", "purge-key-0011", NOW)
    assert refused.value.code == "import_apply_active"
    assert dump(business_items(repo)) == before


@mock_aws
def test_purge_is_allowed_once_an_interrupted_apply_lease_has_expired_and_records_it():
    repo = repo_with_business()
    job = create_job(repo, BUSINESS_ID, "subject-1", "import-key-active-2", records(30))
    _mark_applying(repo, job["importId"], NOW - timedelta(seconds=1))
    result = repo.purge_import_job(BUSINESS_ID, job["importId"], "subject-1", "purge-key-0012", NOW)
    assert result["status"] == "purged"
    evidence = [item for item in all_items(repo) if item.get("action") == "import.purged"][0]
    assert evidence["details"]["priorStatus"] == "applying"


# --- HTTP + authorization ----------------------------------------------------

def _http_purge(runtime, key, groups, import_id, business_id=BUSINESS_ID, subject="subject-1"):
    return handle_platform(platform_event("POST", ROUTE_PURGE, subject, key, None, groups, business_id, import_id), runtime)


@mock_aws
def test_http_purge_requires_platform_authorization_and_reveals_nothing_when_denied(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_GROUP", "cauce-super-admin")
    repo = repo_with_business()
    job = create_job(repo, BUSINESS_ID, "subject-1", "import-key-http-1", pii_rows())
    runtime = RealRuntime(repo)
    before = dump(all_items(repo))
    for groups in ([], ["cauce-super-admin-x"], ["staff"]):
        response = _http_purge(runtime, "purge-key-0013", groups, job["importId"])
        assert response["statusCode"] == 403 and json.loads(response["body"]) == {"error": "forbidden"}
    unknown = _http_purge(runtime, "purge-key-0013", ["staff"], "does-not-exist")
    assert unknown["statusCode"] == 403  # same answer whether or not the import exists
    assert dump(all_items(repo)) == before


@mock_aws
def test_http_purge_happy_path_unknown_import_bad_key_and_active_apply(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_GROUP", "cauce-super-admin")
    repo = repo_with_business()
    job = create_job(repo, BUSINESS_ID, "subject-1", "import-key-http-2", pii_rows())
    runtime = RealRuntime(repo)

    assert _http_purge(runtime, None, SUPER, job["importId"])["statusCode"] == 400  # missing Idempotency-Key
    assert _http_purge(runtime, "short", SUPER, job["importId"])["statusCode"] == 400
    assert _http_purge(runtime, "purge-key-0014", SUPER, "does-not-exist")["statusCode"] == 404

    _mark_applying(repo, job["importId"], NOW + timedelta(seconds=30))
    blocked = _http_purge(runtime, "purge-key-0014", SUPER, job["importId"])
    assert blocked["statusCode"] == 409 and json.loads(blocked["body"])["error"] == "import_apply_active"

    _mark_applying(repo, job["importId"], NOW - timedelta(seconds=1))
    done = _http_purge(runtime, "purge-key-0014", SUPER, job["importId"])
    body = json.loads(done["body"])
    assert done["statusCode"] == 200 and body["status"] == "purged"
    readable = dump(json.loads(done["body"]))
    for secret in (PII_NAME, PII_EMAIL, PII_EXTERNAL_ID):
        assert secret not in readable
    again = _http_purge(runtime, "purge-key-0014", SUPER, job["importId"])
    assert again["statusCode"] == 200 and json.loads(again["body"]) == body

    got = handle_platform(platform_event("GET", ROUTE_GET, "subject-1", None, None, SUPER, BUSINESS_ID, job["importId"]), runtime)
    assert json.loads(got["body"])["status"] == "purged"


@mock_aws
def test_http_purge_of_another_business_is_a_plain_404(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_GROUP", "cauce-super-admin")
    repo = repo_with_business()
    repo.put_tenant_manifest(manifest(OTHER_BUSINESS_ID))
    job = create_job(repo, BUSINESS_ID, "subject-1", "import-key-http-3", pii_rows())
    response = _http_purge(RealRuntime(repo), "purge-key-0015", SUPER, job["importId"], business_id=OTHER_BUSINESS_ID)
    assert response["statusCode"] == 404 and json.loads(response["body"]) == {"error": "not_found"}
    assert repo.import_job_detail(BUSINESS_ID, job["importId"])["status"] == "validated"


def test_purge_service_call_is_authorized_before_touching_the_repository():
    class Boom:
        def __getattr__(self, name):
            raise AssertionError("repository must not be reached")

    with pytest.raises(PlatformForbidden):
        PlatformService(Boom(), lambda: NOW).purge_import("business-1", "subject-1", None, "import-1", "purge-key-0016")
    with pytest.raises(PlatformForbidden):
        PlatformService(Boom(), lambda: NOW).purge_import("business-1", "", "super_admin", "import-1", "purge-key-0016")
