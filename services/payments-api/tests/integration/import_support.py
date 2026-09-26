"""Shared fixtures for the ETL retention/purge and apply integration tests.

Same principle as test_import_jobs.py: a real DynamoRepository against a real
(in-memory, Moto-backed) DynamoDB table -- never a fake repository.
"""

from datetime import datetime, timedelta, timezone
import json
import os
import sys

import boto3

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from payments import imports
from payments.platform import PlatformService


NOW = datetime(2026, 9, 19, 12, 0, tzinfo=timezone.utc)
BUSINESS_ID = "11111111-1111-4111-8111-111111111111"
OTHER_BUSINESS_ID = "22222222-2222-4222-8222-222222222222"
SUPER = ["cauce-super-admin"]

# Distinctive values, so a leak of personal data anywhere is greppable.
PII_NAME = "Ferretería SECRETO del Norte"
PII_EMAIL = "secreto.persona@example.com"
PII_EXTERNAL_ID = "CLI-SECRETO-9931"


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


def record(source_row=2, customer_id="CLI-1", name="Ferretería del Norte", email="cobros@example.com",
           charge_id="FAC-1", amount_minor=1250000, description="Material", due_date="2026-09-30"):
    return {
        "sourceRow": source_row,
        "customer": {"externalId": customer_id, "displayName": name, "email": email},
        "charge": {"externalId": charge_id, "amountMinor": amount_minor, "currency": "MXN", "description": description, "dueDate": due_date},
    }


def records(count, *, customers=None, prefix="FAC"):
    """`count` valid rows; `customers` distinct customers cycled over them
    (default: one customer per row)."""
    customers = customers or count
    return [
        record(source_row=index + 2, customer_id=f"CLI-{index % customers}", name=f"Cliente {index % customers}",
               email=f"cliente{index % customers}@example.com", charge_id=f"{prefix}-{index}", amount_minor=1000 + index)
        for index in range(count)
    ]


def source():
    return {"fileName": "cartera.csv", "format": "csv"}


def profile():
    return {"name": "Cobranza estándar MX", "delimiter": "comma", "dateFormat": "iso", "decimalSeparator": "dot", "currency": "MXN"}


def validate_body(rows):
    return {"source": source(), "profile": profile(), "records": rows}


class RealRuntime:
    """PlatformService over the real Moto-backed repository."""
    def __init__(self, repo, clock=lambda: NOW, **service_kwargs):
        self._service = PlatformService(repo, clock, **service_kwargs)

    def platform_service(self):
        return self._service


def platform_event(method, route, subject, key, body, groups, business_id=BUSINESS_ID, import_id=None):
    return {
        "requestContext": {"http": {"method": method}, "routeKey": route, "authorizer": {"jwt": {"claims": {"sub": subject, "cognito:groups": groups}}}},
        "pathParameters": {"businessId": business_id, **({"importId": import_id} if import_id else {})},
        "queryStringParameters": {},
        "headers": {"Idempotency-Key": key} if key is not None else {},
        "body": json.dumps(body) if body is not None else None,
    }


ROUTE_VALIDATE = "POST /platform/businesses/{businessId}/imports/validate"
ROUTE_APPLY = "POST /platform/businesses/{businessId}/imports/{importId}/apply"
ROUTE_PURGE = "POST /platform/businesses/{businessId}/imports/{importId}/purge"
ROUTE_GET = "GET /platform/businesses/{businessId}/imports/{importId}"


def create_job(repo, business_id, subject_id, key, rows, now=NOW, retention_days=30):
    """Run the exact production path: same domain module, then persist."""
    validation = imports.validate_batch(rows)
    digest = imports.canonical_digest(source(), profile(), rows)
    chunks = imports.chunk_rows(validation["rowResults"])
    return repo.create_import_job(
        business_id, subject_id, key, digest, validation["status"],
        source(), profile(), validation["summary"], validation["issues"], validation["issuesTruncated"],
        chunks, now, rows_expires_at=now + timedelta(days=retention_days),
    )


def all_items(repo):
    """Every item in the table (test-only Scan)."""
    items, kwargs = [], {}
    while True:
        response = repo.table.scan(**kwargs)
        items.extend(response.get("Items", []))
        if not response.get("LastEvaluatedKey"):
            return items
        kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]


def dump(items):
    # ensure_ascii=False on purpose: with the default, any accented character
    # is escaped, so a negative "no PII here" assertion on an accented name
    # would pass vacuously even if the name had leaked.
    return json.dumps(items, default=str, sort_keys=True, ensure_ascii=False)
