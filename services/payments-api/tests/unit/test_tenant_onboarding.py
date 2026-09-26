import json
import os
import sys
from datetime import datetime, timezone

import boto3
from moto import mock_aws
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../scripts"))

import onboard_tenant
from payments.dynamodb import DynamoRepository


BUSINESS_ID = "11111111-1111-4111-8111-111111111111"
CONNECTION_ID = "22222222-2222-4222-8222-222222222222"
MEMBERSHIP_ID = "33333333-3333-4333-8333-333333333333"


@pytest.fixture
def table(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    with mock_aws():
        resource = boto3.resource("dynamodb", region_name="us-east-1")
        yield resource.create_table(
            TableName="payments", BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": name, "AttributeType": "S"}
                for name in ("PK", "SK", "GSI1PK", "GSI1SK")
            ],
            KeySchema=[{"AttributeName": "PK", "KeyType": "HASH"}, {"AttributeName": "SK", "KeyType": "RANGE"}],
            GlobalSecondaryIndexes=[{
                "IndexName": "LookupIndex",
                "KeySchema": [{"AttributeName": "GSI1PK", "KeyType": "HASH"}, {"AttributeName": "GSI1SK", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"},
            }],
        )


def manifest():
    return {
        "business": {"id": BUSINESS_ID, "displayName": "Taller Norte", "folioPrefix": "TN",
                     "branding": {"publicName": "Taller Norte"}},
        "memberships": [{"id": MEMBERSHIP_ID, "subjectId": "owner-1", "role": "owner"}],
        "mercadoPago": {"id": CONNECTION_ID, "providerAccountId": "seller-1",
                        "credentialSecretRef": "arn:credentials", "webhookSecretRef": "arn:webhook"},
    }


def rows(table):
    return sorted(table.scan(ConsistentRead=True)["Items"], key=lambda item: item["SK"])


def manifest_file(tmp_path):
    value = manifest()
    value["mercadoPago"].update(environment="test", credentialSource="test_credentials")
    path = tmp_path / "tenant.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_first_apply_provisions_all_items_through_command(table, tmp_path, capsys):
    path = manifest_file(tmp_path)

    assert onboard_tenant.main([str(path), "--apply", "--table-name", "payments"]) == 0

    output = json.loads(capsys.readouterr().out)
    assert output == {"outcome": "provisioned", "businessId": BUSINESS_ID,
                      "connectionState": "needs_provider_verification", "memberships": 1}
    items = rows(table)
    assert len(items) == 4
    assert {item["entity"] for item in items} == {"business", "membership", "connection", "connection_identity"}
    assert next(item for item in items if item["entity"] == "connection").get("verified_at") is None
    assert next(item for item in items if item["entity"] == "connection_identity")["PK"] == f"CONNECTION_IDENTITY#{CONNECTION_ID}"


def test_repeat_apply_rejects_existing_tenant_without_writes(table, tmp_path, capsys):
    path = manifest_file(tmp_path)
    assert onboard_tenant.main([str(path), "--apply", "--table-name", "payments"]) == 0
    before = rows(table)
    capsys.readouterr()

    assert onboard_tenant.main([str(path), "--apply", "--table-name", "payments"]) == 2

    assert "tenant already exists" in json.loads(capsys.readouterr().err)["error"]
    assert rows(table) == before


def test_repeat_cannot_restore_revoked_membership(table):
    repo = DynamoRepository("payments", resource=boto3.resource("dynamodb", region_name="us-east-1"))
    repo.put_tenant_manifest(manifest())
    table.update_item(
        Key={"PK": f"BUSINESS#{BUSINESS_ID}", "SK": f"MEMBERSHIP#{MEMBERSHIP_ID}"},
        UpdateExpression="SET revoked_at = :when",
        ExpressionAttributeValues={":when": "2026-09-26T00:00:00+00:00"},
    )
    before = rows(table)
    retry = manifest()
    retry["memberships"][0]["id"] = "44444444-4444-4444-8444-444444444444"

    with pytest.raises(ValueError, match="tenant already exists"):
        repo.put_tenant_manifest(retry)

    assert rows(table) == before
    assert repo.membership(BUSINESS_ID, "owner-1") is None


def test_repeat_cannot_reset_verified_connection(table):
    repo = DynamoRepository("payments", resource=boto3.resource("dynamodb", region_name="us-east-1"))
    repo.put_tenant_manifest(manifest())
    repo.mark_connection_verified(CONNECTION_ID, datetime(2026, 9, 26, tzinfo=timezone.utc))
    before = rows(table)

    with pytest.raises(ValueError, match="tenant already exists"):
        repo.put_tenant_manifest(manifest())

    assert rows(table) == before
    assert next(item for item in rows(table) if item["entity"] == "connection")["verified_at"] is not None


def test_conflicting_write_does_not_partially_provision_tenant(table):
    # A leftover connection row makes the transaction fail after the business
    # and membership puts have been prepared, without committing either one.
    existing = {"PK": f"BUSINESS#{BUSINESS_ID}", "SK": f"CONNECTION#{CONNECTION_ID}",
                "entity": "connection", "verified_at": "2026-09-26T00:00:00+00:00"}
    table.put_item(Item=existing)
    repo = DynamoRepository("payments", resource=boto3.resource("dynamodb", region_name="us-east-1"))

    with pytest.raises(ValueError, match="onboarding items conflict"):
        repo.put_tenant_manifest(manifest())

    assert rows(table) == [existing]


def test_connection_id_cannot_be_reused_by_another_business(table):
    repo = DynamoRepository("payments", resource=boto3.resource("dynamodb", region_name="us-east-1"))
    repo.put_tenant_manifest(manifest())
    before = rows(table)
    other = manifest()
    other["business"]["id"] = "55555555-5555-4555-8555-555555555555"
    other["memberships"][0]["id"] = "66666666-6666-4666-8666-666666666666"

    with pytest.raises(ValueError, match="onboarding items conflict"):
        repo.put_tenant_manifest(other)

    assert rows(table) == before
    assert not any(item["PK"] == f"BUSINESS#{other['business']['id']}" for item in rows(table))


def test_duplicate_subjects_are_rejected_before_apply(table, tmp_path, capsys):
    path = manifest_file(tmp_path)
    duplicate = json.loads(path.read_text(encoding="utf-8"))
    duplicate["memberships"].append({"subjectId": " owner-1 ", "role": "staff"})
    path.write_text(json.dumps(duplicate), encoding="utf-8")

    assert onboard_tenant.main([str(path), "--apply", "--table-name", "payments"]) == 2

    assert "distinct subjectId" in json.loads(capsys.readouterr().err)["error"]
    assert rows(table) == []


def test_repository_rejects_duplicate_subjects_before_any_write(table):
    repo = DynamoRepository("payments", resource=boto3.resource("dynamodb", region_name="us-east-1"))
    duplicate = manifest()
    duplicate["memberships"].append({"id": "77777777-7777-4777-8777-777777777777",
                                     "subjectId": " owner-1 ", "role": "staff"})

    with pytest.raises(ValueError, match="distinct subjectId"):
        repo.put_tenant_manifest(duplicate)

    assert rows(table) == []


def test_oversized_manifest_is_rejected_before_any_write(table, tmp_path, capsys):
    repo = DynamoRepository("payments", resource=boto3.resource("dynamodb", region_name="us-east-1"))
    oversized = manifest()
    oversized["memberships"] = [
        {"id": f"{index:08d}-3333-4333-8333-333333333333", "subjectId": f"owner-{index}", "role": "owner"}
        for index in range(98)
    ]
    oversized["mercadoPago"].update(environment="test", credentialSource="test_credentials")
    path = tmp_path / "oversized.json"
    path.write_text(json.dumps(oversized), encoding="utf-8")

    assert onboard_tenant.main([str(path)]) == 2
    assert "at most 97" in json.loads(capsys.readouterr().err)["error"]

    with pytest.raises(ValueError, match="100-item transaction limit"):
        repo.put_tenant_manifest(oversized)

    assert rows(table) == []
