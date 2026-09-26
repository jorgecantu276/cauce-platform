import base64
import json

from payments.imports import ImportIdempotencyConflict, ImportOperationRefused
from payments.platform import PlatformForbidden
from payments.platform_auth import platform_role


def _response(status, body):
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store"},
        "body": json.dumps(body, separators=(",", ":")),
    }


def _body(event):
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode()
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("request body must be an object")
    return value


def _headers(event):
    return {str(key).lower(): str(value) for key, value in (event.get("headers") or {}).items()}


def _subject(event):
    authorizer = (event.get("requestContext") or {}).get("authorizer") or {}
    claims = (authorizer.get("jwt") or {}).get("claims") or {}
    return str(claims.get("sub") or "")


def handle_platform(event, runtime):
    """Every /platform route. Deliberately its own transport module, not a
    branch inside handle_staff: platform access is a different trust
    boundary (a verified JWT group, not tenant membership), and keeping it
    physically separate makes it impossible for a future edit to one to
    silently change the other's authorization.

    This function only translates HTTP <-> PlatformService calls -- it never
    touches DynamoRepository directly (see payments/platform.py for why).
    """
    method = ((event.get("requestContext") or {}).get("http") or {}).get("method", "")
    route_key = (event.get("requestContext") or {}).get("routeKey", "")
    params = event.get("pathParameters") or {}
    query = event.get("queryStringParameters") or {}
    business_id = str(params.get("businessId") or "")
    subject_id = _subject(event)
    # Never read from body/query/headers: this is the one server-verified
    # signal of platform access, and it must come from nowhere else.
    role = platform_role(event)
    key = _headers(event).get("idempotency-key", "")
    try:
        service = runtime.platform_service()
        if method == "POST" and route_key.endswith("/imports/validate"):
            job = service.validate_import(business_id, subject_id, role, key, _body(event))
            return _response(200, job)
        if method == "POST" and route_key.endswith("/imports/{importId}/apply"):
            result = service.apply_import(business_id, subject_id, role, str(params.get("importId") or ""), key)
            if result is None:
                return _response(404, {"error": "not_found"})
            return _response(200, result)
        if method == "POST" and route_key.endswith("/imports/{importId}/purge"):
            result = service.purge_import(business_id, subject_id, role, str(params.get("importId") or ""), key)
            if result is None:
                return _response(404, {"error": "not_found"})
            return _response(200, result)
        if method == "GET" and route_key.endswith("/imports/{importId}"):
            job = service.get_import(business_id, subject_id, role, str(params.get("importId") or ""))
            if job is None:
                return _response(404, {"error": "not_found"})
            return _response(200, job)
        if method == "GET" and route_key.endswith("/imports"):
            limit = query.get("limit")
            page = service.list_imports(business_id, subject_id, role, int(limit) if limit else 50, query.get("cursor"))
            return _response(200, page)
        return _response(404, {"error": "not_found"})
    except PlatformForbidden:
        # Generic on purpose: never confirms or denies whether businessId
        # itself is real to a caller who isn't platform staff.
        return _response(403, {"error": "forbidden"})
    except ImportOperationRefused as refused:
        # The import exists but its state forbids this operation. `code` and
        # `status` are fixed vocabulary (never row data), so the UI can say
        # exactly why without the server echoing anything personal.
        return _response(409, {"error": refused.code, "status": refused.status})
    except ImportIdempotencyConflict:
        # Must be caught before the generic ValueError branch below: this
        # is a distinct type specifically so a conflicting-payload retry
        # (409, the caller's own key/payload mismatch) is never confused
        # with a structurally invalid request (400).
        return _response(409, {"error": "operation_conflict"})
    except (ValueError, TypeError, json.JSONDecodeError):
        return _response(400, {"error": "invalid_request"})
    except RuntimeError:
        return _response(409, {"error": "operation_conflict"})
