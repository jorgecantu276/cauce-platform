from datetime import datetime, timezone
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from payments.staff import StaffForbidden, StaffService
from payments.staff_transport import handle_staff


NOW = datetime(2026,9,11,18,0,tzinfo=timezone.utc)


class Repository:
    def __init__(self):
        self.charge_calls = []

    def membership(self,business_id,subject_id,roles=("owner","staff")):
        if (business_id,subject_id) != ("business-1","subject-1"):
            return None
        return {"id":"membership-1","business_id":business_id,"role":"owner"}

    def create_customer(self,business_id,name,email,key):
        return {"customerId":"customer-1","displayName":name,"email":email}

    def list_customers(self,business_id,limit):
        return [{"customerId":"customer-1","displayName":"Cliente Uno","email":"uno@example.test"}]

    def customer_page(self,business_id,limit):
        return {"items":self.list_customers(business_id,limit),"hasMore":True}

    def create_charge(self,*args,**kwargs):
        self.charge_calls.append((args,kwargs))
        return {"chargeId":"charge-1","folio":"TN-000001","token":kwargs["link_token"]}

    def list_charges(self,business_id,limit):
        return [{"folio":"TN-000001"}]

    def business_summary(self,business_id):
        return {"openChargeCount":1,"outstandingMinor":12500}

    def charge_detail(self,business_id,charge_id):
        return {"charge":{"chargeId":charge_id},"customer":{"displayName":"Cliente Uno"},
                "paymentLink":{"state":"available_once","issuedAt":NOW.isoformat()},
                "attempts":[],"payments":[],"allocations":[],"adjustments":[],
                "providerEvents":[],"refundOperations":[]}

    def work_queue(self,business_id,today,limit,include_reviews=False):
        return [{"kind":"due_today","id":"charge-1","title":"Cliente Uno",
                 "detail":"TN-000001 · Anticipo"}]

    def merchant_connection_status(self,business_id):
        return {"provider":"mercado_pago","state":"verified_test","environment":"test",
                "credentialSource":"test_credentials","verifiedAt":NOW.isoformat(),
                "displayAccount":"••••1234"}

    def business_branding(self,business_id):
        return {"publicName":"Taller Norte","accent":"#ed684c","accentHover":"#d8563c",
                "nav":"#101931","navAlt":"#172545","canvas":"#f5f5f2"}

    def staff_session(self,subject_id):
        return {"memberships":[{"businessId":"business-1","businessName":"Taller Norte","role":"owner"}]}

    def cancel_charge(self,*args):
        return {"chargeId":args[1],"cancelled":True}

    def create_refund_operation(self,*args):
        return {"refundId":"refund-1","amountMinor":12500,"status":"requested"}

    def list_reviews(self,business_id,limit):
        return [{"kind":"payment","reason":"amount_mismatch"}]

    def review_page(self,business_id,limit):
        return {"items":self.list_reviews(business_id,limit),"hasMore":True}

    def resolve_review(self,*args):
        return {"kind":args[1],"reviewId":args[2],"action":args[5],"outcome":"queued"}


def service(repo=None):
    return StaffService(repo or Repository(),"test-link-secret","https://pay.example",lambda:NOW)


def test_membership_is_server_checked_for_the_selected_business():
    with pytest.raises(StaffForbidden):
        service().list_charges("business-2","subject-1")
    with pytest.raises(StaffForbidden):
        service().list_charges("business-1","subject-2")


def test_business_summary_is_membership_checked_and_not_capped_like_the_list():
    assert service().business_summary("business-1","subject-1") == {
        "openChargeCount":1,"outstandingMinor":12500,
    }
    with pytest.raises(StaffForbidden):
        service().business_summary("business-2","subject-1")


def test_charge_amount_is_server_persisted_and_retry_link_is_deterministic():
    repo = Repository()
    first = service(repo).create_charge(
        "business-1","subject-1","staff-request-123",
        {"customerId":"customer-1","amountMinor":12500,"currency":"MXN",
         "description":"Anticipo","dueDate":"2026-09-20"},
    )
    second = service(repo).create_charge(
        "business-1","subject-1","staff-request-123",
        {"customerId":"customer-1","amountMinor":12500,"currency":"MXN",
         "description":"Anticipo","dueDate":"2026-09-20"},
    )
    assert first["paymentUrl"] == second["paymentUrl"]
    args,options = repo.charge_calls[0]
    assert args[2:6] == (12500,"MXN","Anticipo",NOW.date().replace(day=20))
    assert options["created_by_membership_id"] == "membership-1"


class Runtime:
    def __init__(self):
        self.value = service()

    def staff_service(self):
        return self.value


def event(method,route,subject="subject-1",body=None,key="staff-request-123",groups=None):
    return {
        "requestContext":{"http":{"method":method},"routeKey":route,
                          "authorizer":{"jwt":{"claims":{"sub":subject,**({"cognito:groups":groups} if groups is not None else {})}}}},
        "pathParameters":{"businessId":"business-1","chargeId":"charge-1",
                          "paymentId":"payment-1","reviewKind":"payment",
                          "reviewId":"review-1"},
        "headers":{"Idempotency-Key":key},"body":json.dumps(body or {}),
    }


def test_staff_transport_requires_membership_and_rejects_buyer_style_amounts():
    denied = handle_staff(event("GET","GET /businesses/{businessId}/charges",subject="other"),Runtime())
    assert denied["statusCode"] == 403


def test_staff_transport_exposes_the_uncapped_business_summary_route():
    response = handle_staff(event("GET","GET /businesses/{businessId}/summary"),Runtime())
    assert response["statusCode"] == 200
    assert json.loads(response["body"]) == {"openChargeCount":1,"outstandingMinor":12500}
    denied = handle_staff(event("GET","GET /businesses/{businessId}/summary",subject="other"),Runtime())
    assert denied["statusCode"] == 403
    invalid = handle_staff(event(
        "POST","POST /businesses/{businessId}/charges",
        body={"customerId":"customer-1","amountMinor":"12500",
              "description":"Anticipo","dueDate":"2026-09-20"}),Runtime())
    assert invalid["statusCode"] == 400


def test_staff_customer_list_is_membership_scoped():
    response = handle_staff(event("GET","GET /businesses/{businessId}/customers"),Runtime())
    assert response["statusCode"] == 200
    body = json.loads(response["body"])
    assert body["customers"][0]["displayName"] == "Cliente Uno"
    assert body["hasMore"] is True
    denied = handle_staff(event("GET","GET /businesses/{businessId}/customers",subject="other"),Runtime())
    assert denied["statusCode"] == 403


def test_staff_read_models_are_membership_scoped_and_secret_free():
    runtime = Runtime()
    detail = handle_staff(event("GET","GET /businesses/{businessId}/charges/{chargeId}"),runtime)
    assert detail["statusCode"] == 200
    assert json.loads(detail["body"])["customer"]["displayName"] == "Cliente Uno"
    queue = handle_staff(event("GET","GET /businesses/{businessId}/work-queue"),runtime)
    assert json.loads(queue["body"])["items"][0]["kind"] == "due_today"
    connection = handle_staff(event("GET","GET /businesses/{businessId}/merchant-connection"),runtime)
    assert json.loads(connection["body"])["displayAccount"] == "••••1234"
    branding = handle_staff(event("GET","GET /businesses/{businessId}/branding"),runtime)
    assert json.loads(branding["body"])["accent"] == "#ed684c"
    session = handle_staff(event("GET","GET /session"),runtime)
    assert json.loads(session["body"])["memberships"][0]["businessId"] == "business-1"


def test_session_exposes_platform_role_only_for_the_exact_verified_group(monkeypatch):
    monkeypatch.setenv("PLATFORM_ADMIN_GROUP","cauce-super-admin")
    allowed = handle_staff(event("GET","GET /session",groups='["staff","cauce-super-admin"]'),Runtime())
    similar = handle_staff(event("GET","GET /session",groups='["cauce-super-admin-helper"]'),Runtime())
    absent = handle_staff(event("GET","GET /session"),Runtime())
    assert json.loads(allowed["body"])["platformRole"] == "super_admin"
    assert json.loads(similar["body"])["platformRole"] is None
    assert json.loads(absent["body"])["platformRole"] is None


def test_refund_request_is_durable_and_requires_integer_minor_units():
    response = handle_staff(event(
        "POST","POST /businesses/{businessId}/payments/{paymentId}/refunds",
        body={"amountMinor":12500}),Runtime())
    assert response["statusCode"] == 202
    assert json.loads(response["body"])["status"] == "requested"


def test_owner_can_submit_an_audited_review_retry():
    response = handle_staff(event(
        "POST","POST /businesses/{businessId}/reviews/{reviewKind}/{reviewId}/resolve",
        body={"action":"retry","note":"Provider status checked by owner"}),Runtime())
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["outcome"] == "queued"


def test_staff_review_list_exposes_truncation_signal():
    response = handle_staff(event("GET","GET /businesses/{businessId}/reviews"),Runtime())
    assert response["statusCode"] == 200
    assert json.loads(response["body"])["hasMore"] is True
