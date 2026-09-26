from datetime import datetime, timezone
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from payments.imports import ImportIdempotencyConflict
from payments.platform import PlatformForbidden, PlatformService
from payments.platform_transport import handle_platform


NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def _record(source_row=2, external_id="CLI-1", name="Ferretería del Norte", email="cobros@example.com",
            charge_id="FAC-1", amount_minor=1250000, currency="MXN", description="Material", due_date="2026-09-30"):
    return {
        "sourceRow": source_row,
        "customer": {"externalId": external_id, "displayName": name, "email": email},
        "charge": {"externalId": charge_id, "amountMinor": amount_minor, "currency": currency, "description": description, "dueDate": due_date},
    }


def _body(records=None):
    return {
        "source": {"fileName": "cartera.csv", "format": "csv"},
        "profile": {"name": "Cobranza estándar MX", "delimiter": "comma", "dateFormat": "iso", "decimalSeparator": "dot", "currency": "MXN"},
        "records": records if records is not None else [_record()],
    }


class Repository:
    def __init__(self):
        self.create_calls = []

    def create_import_job(self, business_id, subject_id, key, digest, status, source, profile, summary, issues, truncated, chunks, now, *, rows_expires_at):
        self.create_calls.append((business_id, subject_id, key, digest, status, summary))
        return {
            "importId": "import-1", "businessId": business_id, "status": status, "source": source, "profile": profile,
            "summary": summary, "issues": issues, "issuesTruncated": truncated,
            "createdAt": now.isoformat(), "validatedAt": now.isoformat(), "createdBySubject": subject_id,
        }

    def import_job_detail(self, business_id, import_id, now=None):
        if import_id != "import-1":
            return None
        return {
            "importId": "import-1", "businessId": business_id, "status": "validated",
            "source": {"fileName": "cartera.csv", "format": "csv"},
            "profile": {"name": "p", "delimiter": "comma", "dateFormat": "iso", "decimalSeparator": "dot", "currency": "MXN"},
            "summary": {"inputRows": 1, "validRows": 1, "errorRows": 0, "totalMinor": 1250000},
            "issues": [], "issuesTruncated": False, "createdAt": NOW.isoformat(), "validatedAt": NOW.isoformat(), "createdBySubject": "subject-1",
        }

    def list_import_jobs(self, business_id, limit, cursor):
        return {"items": [self.import_job_detail(business_id, "import-1")], "hasMore": False, "cursor": None}


def service(repo=None):
    return PlatformService(repo or Repository(), lambda: NOW)


# --- PlatformService.authorize --------------------------------------------

def test_no_platform_role_is_forbidden():
    with pytest.raises(PlatformForbidden):
        service().validate_import("business-1", "subject-1", None, "import-key-123", _body())


def test_wrong_role_string_is_forbidden():
    with pytest.raises(PlatformForbidden):
        service().validate_import("business-1", "subject-1", "owner", "import-key-123", _body())


def test_missing_subject_is_forbidden_even_with_the_right_role():
    with pytest.raises(PlatformForbidden):
        service().validate_import("business-1", "", "super_admin", "import-key-123", _body())


def test_exact_role_can_validate():
    repo = Repository()
    result = service(repo).validate_import("business-1", "subject-1", "super_admin", "import-key-123", _body())
    assert result["status"] == "validated"
    assert repo.create_calls[0][0] == "business-1"


def test_get_and_list_also_require_authorization():
    with pytest.raises(PlatformForbidden):
        service().get_import("business-1", "subject-1", "staff", "import-1")
    with pytest.raises(PlatformForbidden):
        service().list_imports("business-1", "subject-1", None, 50, None)


# --- request-shape validation flows through PlatformService ---------------

def test_invalid_idempotency_key_is_rejected():
    with pytest.raises(ValueError):
        service().validate_import("business-1", "subject-1", "super_admin", "short", _body())


def test_non_object_body_is_rejected():
    with pytest.raises(ValueError):
        service().validate_import("business-1", "subject-1", "super_admin", "import-key-123", "not-a-dict")


def test_a_batch_with_errors_still_returns_invalid_not_an_exception():
    repo = Repository()
    result = service(repo).validate_import("business-1", "subject-1", "super_admin", "import-key-123", _body([_record(amount_minor=-1)]))
    assert result["status"] == "invalid"


# --- HTTP transport ---------------------------------------------------------

def event(method, route, subject="subject-1", body=None, key="import-key-123", groups=None, path_params=None, query=None):
    claims = {"sub": subject}
    if groups is not None:
        claims["cognito:groups"] = groups
    return {
        "requestContext": {"http": {"method": method}, "routeKey": route, "authorizer": {"jwt": {"claims": claims}}},
        "pathParameters": {"businessId": "business-1", "importId": "import-1", **(path_params or {})},
        "queryStringParameters": query or {},
        "headers": {"Idempotency-Key": key},
        "body": json.dumps(body if body is not None else _body()),
    }


class Runtime:
    def __init__(self, repo=None):
        self.value = service(repo)

    def platform_service(self):
        return self.value


def test_transport_denies_without_the_exact_verified_group(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_GROUP", "cauce-super-admin")
    response = handle_platform(event(
        "POST", "POST /platform/businesses/{businessId}/imports/validate",
        groups=["cauce-super-admin-helper"],
    ), Runtime())
    assert response["statusCode"] == 403
    assert json.loads(response["body"]) == {"error": "forbidden"}


def test_platform_role_cannot_be_supplied_by_the_client(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_GROUP", "cauce-super-admin")
    body = _body()
    body["platformRole"] = "super_admin"  # must be ignored: only the verified JWT claim counts
    response = handle_platform(event(
        "POST", "POST /platform/businesses/{businessId}/imports/validate", body=body,
    ), Runtime())
    assert response["statusCode"] == 403


def test_transport_allows_the_exact_verified_group(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_GROUP", "cauce-super-admin")
    response = handle_platform(event(
        "POST", "POST /platform/businesses/{businessId}/imports/validate",
        groups=["cauce-super-admin"],
    ), Runtime())
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["status"] == "validated"
    assert body["importId"] == "import-1"
    assert response["headers"]["Cache-Control"] == "no-store"


def test_get_import_route(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_GROUP", "cauce-super-admin")
    response = handle_platform(event(
        "GET", "GET /platform/businesses/{businessId}/imports/{importId}", groups=["cauce-super-admin"],
    ), Runtime())
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["importId"] == "import-1"


def test_get_import_route_404_for_an_unknown_id(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_GROUP", "cauce-super-admin")
    response = handle_platform(event(
        "GET", "GET /platform/businesses/{businessId}/imports/{importId}", groups=["cauce-super-admin"],
        path_params={"importId": "does-not-exist"},
    ), Runtime())
    assert response["statusCode"] == 404


def test_list_imports_route(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_GROUP", "cauce-super-admin")
    response = handle_platform(event(
        "GET", "GET /platform/businesses/{businessId}/imports", groups=["cauce-super-admin"], query={"limit": "10"},
    ), Runtime())
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["items"][0]["importId"] == "import-1"
    assert body["hasMore"] is False


def test_invalid_idempotency_key_yields_400(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_GROUP", "cauce-super-admin")
    response = handle_platform(event(
        "POST", "POST /platform/businesses/{businessId}/imports/validate",
        groups=["cauce-super-admin"], key="short",
    ), Runtime())
    assert response["statusCode"] == 400


class ConflictingRepository(Repository):
    """Isolated, fast check that handle_platform's except-clause ordering
    itself converts ImportIdempotencyConflict to 409 -- not a substitute for
    the real-Moto HTTP tests in test_import_jobs.py, which prove DynamoDB
    actually raises this type in the first place."""
    def create_import_job(self, *args, **kwargs):
        raise ImportIdempotencyConflict("idempotency key was used with a different import payload")


def test_import_idempotency_conflict_is_409_not_400(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_GROUP", "cauce-super-admin")
    response = handle_platform(event(
        "POST", "POST /platform/businesses/{businessId}/imports/validate", groups=["cauce-super-admin"],
    ), Runtime(ConflictingRepository()))
    assert response["statusCode"] == 409
    assert json.loads(response["body"]) == {"error": "operation_conflict"}
