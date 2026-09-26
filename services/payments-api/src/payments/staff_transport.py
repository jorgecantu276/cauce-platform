import base64
import json

from payments.platform_auth import platform_role as _platform_role
from payments.staff import StaffForbidden


def _response(status, body):
    return {
        "statusCode":status,
        "headers":{"Content-Type":"application/json; charset=utf-8",
                   "Cache-Control":"no-store"},
        "body":json.dumps(body,separators=(",",":")),
    }


def _body(event):
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode()
    value = json.loads(raw)
    if not isinstance(value,dict):
        raise ValueError("request body must be an object")
    return value


def _headers(event):
    return {str(k).lower():str(v) for k,v in (event.get("headers") or {}).items()}


def _subject(event):
    authorizer = (event.get("requestContext") or {}).get("authorizer") or {}
    claims = (authorizer.get("jwt") or {}).get("claims") or {}
    return str(claims.get("sub") or "")


def handle_staff(event, runtime):
    method = ((event.get("requestContext") or {}).get("http") or {}).get("method","")
    route_key = (event.get("requestContext") or {}).get("routeKey","")
    params = event.get("pathParameters") or {}
    business_id = str(params.get("businessId") or "")
    subject_id = _subject(event)
    key = _headers(event).get("idempotency-key","")
    try:
        service = runtime.staff_service()
        if method == "GET" and route_key.endswith("/session"):
            session = service.session(subject_id)
            return _response(200,{**session,"platformRole":_platform_role(event)})
        if method == "POST" and route_key.endswith("/customers"):
            return _response(201,service.create_customer(
                business_id,subject_id,key,_body(event)
            ))
        if method == "POST" and route_key.endswith("/charges"):
            return _response(201,service.create_charge(
                business_id,subject_id,key,_body(event)
            ))
        if method == "GET" and route_key.endswith("/customers"):
            page = service.list_customers(business_id,subject_id,200)
            return _response(200,{"customers":page["items"],"hasMore":page["hasMore"]})
        if method == "GET" and route_key.endswith("/charges"):
            return _response(200,{"charges":service.list_charges(
                business_id,subject_id
            )})
        if method == "GET" and route_key.endswith("/summary"):
            return _response(200,service.business_summary(business_id,subject_id))
        if method == "GET" and route_key.endswith("/charges/{chargeId}"):
            detail = service.charge_detail(business_id,subject_id,str(params.get("chargeId") or ""))
            if detail is None:
                return _response(404,{"error":"not_found"})
            return _response(200,detail)
        if method == "GET" and route_key.endswith("/work-queue"):
            return _response(200,{"items":service.work_queue(business_id,subject_id)})
        if method == "GET" and route_key.endswith("/merchant-connection"):
            return _response(200,service.merchant_connection(business_id,subject_id))
        if method == "GET" and route_key.endswith("/branding"):
            return _response(200,service.branding(business_id,subject_id))
        if method == "POST" and route_key.endswith("/cancel"):
            return _response(200,service.cancel_charge(
                business_id,subject_id,str(params.get("chargeId") or ""),key
            ))
        if method == "POST" and route_key.endswith("/refunds"):
            return _response(202,service.request_refund(
                business_id,subject_id,str(params.get("paymentId") or ""),key,_body(event)
            ))
        if method == "GET" and route_key.endswith("/reviews"):
            page = service.list_reviews(business_id,subject_id,200)
            return _response(200,{"reviews":page["items"],"hasMore":page["hasMore"]})
        if method == "POST" and route_key.endswith("/resolve"):
            return _response(200,service.resolve_review(
                business_id,subject_id,str(params.get("reviewKind") or ""),
                str(params.get("reviewId") or ""),key,_body(event)
            ))
        return _response(404,{"error":"not_found"})
    except StaffForbidden:
        return _response(403,{"error":"forbidden"})
    except (ValueError,TypeError,json.JSONDecodeError):
        return _response(400,{"error":"invalid_request"})
    except RuntimeError:
        return _response(409,{"error":"operation_conflict"})
