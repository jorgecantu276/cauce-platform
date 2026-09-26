"""Authenticated staff operations for the smallest collections workflow."""

import base64
from datetime import date
import hashlib
import hmac
import re

from payments.service import SUBMISSION_KEY_RE


EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class StaffForbidden(Exception):
    pass


class StaffService:
    def __init__(self, repository, link_token_secret, public_base_url, clock):
        if not link_token_secret:
            raise RuntimeError("secure link token secret is required")
        self.repository = repository
        self.link_token_secret = link_token_secret.encode()
        self.public_base_url = public_base_url.rstrip("/")
        self.clock = clock

    def authorize(self, business_id, subject_id, roles=("owner","staff")):
        if not business_id or not subject_id:
            raise StaffForbidden()
        membership = self.repository.membership(business_id,subject_id,roles)
        if membership is None:
            raise StaffForbidden()
        return membership

    @staticmethod
    def _creation_key(value):
        if not isinstance(value,str) or not SUBMISSION_KEY_RE.fullmatch(value):
            raise ValueError("invalid idempotency key")
        return value

    def _link_token(self, business_id, creation_key):
        digest = hmac.new(
            self.link_token_secret,
            (business_id + "\n" + creation_key).encode(),
            hashlib.sha256,
        ).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()

    def create_customer(self, business_id, subject_id, creation_key, body):
        self.authorize(business_id,subject_id)
        key = self._creation_key(creation_key)
        email = str(body.get("email") or "").strip() or None
        if email and (len(email) > 254 or not EMAIL_RE.fullmatch(email)):
            raise ValueError("invalid email")
        return self.repository.create_customer(
            business_id,body.get("displayName"),email,key
        )

    def create_charge(self, business_id, subject_id, creation_key, body):
        membership = self.authorize(business_id,subject_id)
        key = self._creation_key(creation_key)
        amount_minor = body.get("amountMinor")
        if not isinstance(amount_minor,int) or isinstance(amount_minor,bool):
            raise ValueError("amountMinor must be an integer")
        try:
            due_date = date.fromisoformat(str(body.get("dueDate") or ""))
        except ValueError as exc:
            raise ValueError("invalid dueDate") from exc
        token = self._link_token(business_id,key)
        created = self.repository.create_charge(
            business_id,body.get("customerId"),amount_minor,
            body.get("currency") or "MXN",body.get("description"),due_date,
            created_by_membership_id=membership["id"],creation_key=key,
            link_token=token,
        )
        return {
            "chargeId":created["chargeId"],"folio":created["folio"],
            "paymentUrl":self.public_base_url + "/pay/" + created["token"],
        }

    def list_charges(self, business_id, subject_id, limit=100):
        self.authorize(business_id,subject_id)
        return self.repository.list_charges(business_id,limit)

    def charge_detail(self, business_id, subject_id, charge_id):
        self.authorize(business_id,subject_id)
        return self.repository.charge_detail(business_id,charge_id)

    def list_customers(self, business_id, subject_id, limit=100):
        self.authorize(business_id,subject_id)
        return self.repository.customer_page(business_id,limit)

    def business_summary(self, business_id, subject_id):
        self.authorize(business_id,subject_id)
        return self.repository.business_summary(business_id)

    def work_queue(self, business_id, subject_id, limit=50):
        membership = self.authorize(business_id,subject_id)
        return self.repository.work_queue(
            business_id,self.clock().date(),limit,
            include_reviews=membership["role"] == "owner",
        )

    def merchant_connection(self, business_id, subject_id):
        self.authorize(business_id,subject_id)
        return self.repository.merchant_connection_status(business_id)

    def branding(self, business_id, subject_id):
        self.authorize(business_id,subject_id)
        return self.repository.business_branding(business_id)

    def session(self, subject_id):
        if not subject_id:
            raise StaffForbidden()
        return self.repository.staff_session(subject_id)

    def cancel_charge(self, business_id, subject_id, charge_id, operation_key):
        membership = self.authorize(business_id,subject_id)
        key = self._creation_key(operation_key)
        return self.repository.cancel_charge(
            business_id,charge_id,membership["id"],key,self.clock()
        )

    def request_refund(self, business_id, subject_id, payment_id, operation_key, body):
        membership = self.authorize(business_id,subject_id,("owner",))
        key = self._creation_key(operation_key)
        amount = body.get("amountMinor")
        if amount is not None and (not isinstance(amount,int) or isinstance(amount,bool)):
            raise ValueError("amountMinor must be an integer")
        return self.repository.create_refund_operation(
            business_id,payment_id,membership["id"],key,amount,self.clock()
        )

    def list_reviews(self, business_id, subject_id, limit=100):
        self.authorize(business_id,subject_id,("owner",))
        return self.repository.review_page(business_id,limit)

    def resolve_review(self, business_id, subject_id, review_kind, review_id,
                       operation_key, body):
        membership = self.authorize(business_id,subject_id,("owner",))
        key = self._creation_key(operation_key)
        return self.repository.resolve_review(
            business_id,review_kind,review_id,membership["id"],key,
            body.get("action"),body.get("note"),self.clock()
        )
