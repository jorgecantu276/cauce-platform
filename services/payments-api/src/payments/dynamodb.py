"""DynamoDB persistence for the canonical payment slice.

The repository deliberately stores small, typed entity items rather than one
tenant JSON document.  Financial mutations use DynamoDB transactions and
conditional writes; read models are assembled from tenant-scoped items.
"""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
import base64
import hashlib
import json
import secrets
import time
import uuid

from botocore.exceptions import ClientError

from payments import imports as imports_module
from payments.imports import ImportIdempotencyConflict, ImportOperationRefused
from payments.models import Charge, MerchantConnection, MerchantRuntimeConfig, PaymentAttempt


def _uuid():
    return str(uuid.uuid4())


_MAX_CONFLICT_RETRIES = 5


class _ApplyLeaseLost(Exception):
    """This apply slice no longer holds the job's lease (it expired and
    another worker took over, or the job left `applying`). The slice stops
    without writing anything else."""


class _SliceFull(Exception):
    """This invocation reached its row or time budget; not an error."""


def _backoff_sleep(attempt):
    """Jittered exponential backoff between bounded conflict retries.

    Kept short (well under a second even at the cap) since this only ever
    runs inside a single Lambda invocation's own budget, retrying a write
    whose condition is expected to resolve within a couple of attempts once
    the winner's transaction has committed -- not a queue-worker backoff.
    """
    base = min(0.02 * (2 ** (attempt - 1)), 0.5)
    time.sleep(base * (0.5 + secrets.randbelow(1000) / 1000))


def _iso(value):
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value.isoformat()
    return value.astimezone(timezone.utc).isoformat()


def _date(value):
    return value if isinstance(value, date) else date.fromisoformat(value)


def _datetime(value):
    return value if isinstance(value, datetime) else datetime.fromisoformat(value)


def _business_key(business_id):
    return f"BUSINESS#{business_id}"


def _plain(value):
    """Undo boto3's number handling for stored import rows: DynamoDB returns
    every number as Decimal, but the domain rules (and money) require whole
    ints. A non-integral number is never coerced -- it is refused."""
    if isinstance(value, Decimal):
        if value != value.to_integral_value():
            raise ValueError("stored import row contains a non-integral number")
        return int(value)
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    return value


def _bounded_page(values, limit):
    """Return a capped UI page while making truncation explicit."""
    page_size = min(max(int(limit), 1), 200)
    return {"items": values[:page_size], "hasMore": len(values) > page_size}


def _encode_import_cursor(last_evaluated_key):
    return base64.urlsafe_b64encode(json.dumps(last_evaluated_key, separators=(",", ":")).encode()).decode()


def _decode_import_cursor(cursor, business_id):
    """Decode an opaque pagination cursor and confirm it actually belongs to
    this business before ever handing it to DynamoDB as ExclusiveStartKey --
    a forged or stale cursor for a different partition must be rejected, not
    silently followed."""
    try:
        decoded = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
    except Exception as error:
        raise ValueError("invalid pagination cursor") from error
    if not isinstance(decoded, dict) or decoded.get("PK") != _business_key(business_id):
        raise ValueError("invalid pagination cursor")
    return decoded


class DynamoRepository:
    """Pilot DynamoDB repository.

    `LookupIndex` contains server-generated external identifiers only.  Every
    staff read uses the business partition, even after a subject-index lookup.
    """

    def __init__(self, table_name, *, webhook_connection_id=None, resource=None):
        if not table_name:
            raise ValueError("table_name is required")
        if resource is None:
            import boto3
            resource = boto3.resource("dynamodb")
        self.table_name = table_name
        self.table = resource.Table(table_name)
        self.client = self.table.meta.client
        self.webhook_connection_id = webhook_connection_id

    @staticmethod
    def _charge(item):
        return Charge(
            id=item["id"], business_id=item["business_id"], customer_id=item["customer_id"],
            folio=item["folio"], amount_minor=int(item["amount_minor"]),
            outstanding_minor=int(item["outstanding_minor"]), currency=item["currency"],
            description=item["description"], due_date=_date(item["due_date"]),
            merchant_display_name=item.get("merchant_display_name"),
            public_branding=item.get("public_branding"),
            cancelled=bool(item.get("cancelled_at")),
        )

    @staticmethod
    def _connection(item):
        return MerchantConnection(
            id=item["id"], business_id=item["business_id"], provider=item["provider"],
            provider_account_id=item["provider_account_id"], environment=item["environment"],
            verified_at=_datetime(item["verified_at"]) if item.get("verified_at") else None,
        )

    @staticmethod
    def _attempt(item):
        return PaymentAttempt(
            id=item["id"], business_id=item["business_id"], charge_id=item["charge_id"],
            merchant_connection_id=item["merchant_connection_id"], operation_key=item["operation_key"],
            submission_key=item["submission_key"], expected_amount_minor=int(item["expected_amount_minor"]),
            currency=item["currency"], provider=item["provider"], environment=item["environment"],
            status=item["status"], provider_preference_id=item.get("provider_preference_id"),
            checkout_url=item.get("checkout_url"),
        )

    def _get(self, business_id, sk):
        return self.table.get_item(Key={"PK": _business_key(business_id), "SK": sk}, ConsistentRead=True).get("Item")

    def _lookup(self, identity):
        response = self.table.query(
            IndexName="LookupIndex", KeyConditionExpression="#pk = :pk",
            ExpressionAttributeNames={"#pk": "GSI1PK"},
            ExpressionAttributeValues={":pk": identity}, Limit=2,
        )
        items = response.get("Items", [])
        return items[0] if len(items) == 1 else None

    def _items(self, business_id, prefix):
        from boto3.dynamodb.conditions import Key
        query = {
            "KeyConditionExpression": Key("PK").eq(_business_key(business_id)) & Key("SK").begins_with(prefix),
            "ConsistentRead": True,
        }
        values = []
        while True:
            response = self.table.query(**query)
            values.extend(response.get("Items", []))
            key = response.get("LastEvaluatedKey")
            if not key:
                return values
            query["ExclusiveStartKey"] = key

    @staticmethod
    def _conditional(error):
        return error.response.get("Error", {}).get("Code") in {
            "ConditionalCheckFailedException", "TransactionCanceledException"
        }

    def _put(self, item, *, condition=None, names=None, values=None):
        options = {"Item": item}
        if condition:
            options["ConditionExpression"] = condition
        if names:
            options["ExpressionAttributeNames"] = names
        if values:
            options["ExpressionAttributeValues"] = values
        return self.table.put_item(**options)

    def put_tenant_manifest(self, manifest):
        """Idempotently provision validated onboarding data without secrets."""
        business = manifest["business"]
        connection = manifest["mercadoPago"]
        now = _iso(datetime.now(timezone.utc))
        pk = _business_key(business["id"])
        self.table.put_item(Item={
            "PK": pk, "SK": "BUSINESS", "entity": "business", "id": business["id"],
            "business_id": business["id"], "display_name": business["displayName"],
            "folio_prefix": business["folioPrefix"], "branding": business["branding"], "updated_at": now,
        })
        for membership in manifest["memberships"]:
            self.table.put_item(Item={
                "PK": pk, "SK": f"MEMBERSHIP#{membership['id']}", "entity": "membership",
                "id": membership["id"], "business_id": business["id"], "subject_id": membership["subjectId"],
                "role": membership["role"], "revoked_at": None, "GSI2PK": f"SUBJECT#{membership['subjectId']}",
                "GSI2SK": pk, "updated_at": now,
            })
        connection_item = {
            "PK": pk, "SK": f"CONNECTION#{connection['id']}", "entity": "connection", "id": connection["id"],
            "business_id": business["id"], "provider": "mercado_pago", "environment": "test",
            "provider_account_id": connection["providerAccountId"],
            "credential_secret_ref": connection["credentialSecretRef"],
            "webhook_secret_ref": connection["webhookSecretRef"], "credential_source": "test_credentials",
            "verified_at": None, "disabled_at": None, "GSI1PK": f"CONNECTION#{connection['id']}",
            "GSI1SK": "CONNECTION", "updated_at": now,
        }
        self.table.put_item(Item=connection_item)

    def membership(self, business_id, subject_id, roles=("owner", "staff")):
        for value in self._items(business_id, "MEMBERSHIP#"):
            if value["subject_id"] == subject_id and not value.get("revoked_at") and value["role"] in roles:
                return {"id": value["id"], "business_id": business_id, "subject_id": subject_id, "role": value["role"]}
        return None

    def staff_session(self, subject_id):
        from boto3.dynamodb.conditions import Key
        values = self.table.query(
            IndexName="SubjectIndex", KeyConditionExpression=Key("GSI2PK").eq(f"SUBJECT#{subject_id}"),
        ).get("Items", [])
        memberships = []
        for membership in values:
            if membership.get("revoked_at"):
                continue
            business = self._get(membership["business_id"], "BUSINESS")
            if business:
                memberships.append({"businessId": membership["business_id"], "businessName": business["display_name"], "role": membership["role"]})
        return {"memberships": sorted(memberships, key=lambda value: value["businessName"]) }

    def create_customer(self, business_id, display_name, email, creation_key, *, _attempt=1):
        display_name = str(display_name or "").strip()
        email = str(email or "").strip() or None
        if not display_name or len(display_name) > 160:
            raise ValueError("display_name must contain 1-160 characters")
        existing = self._get(business_id, f"CUSTOMER_KEY#{creation_key}") if creation_key else None
        if existing:
            customer = self._get(business_id, f"CUSTOMER#{existing['customer_id']}")
            if not customer or customer["display_name"] != display_name or customer.get("email") != email:
                raise ValueError("idempotency key was used with different customer data")
            return {"customerId": customer["id"], "displayName": customer["display_name"], "email": customer.get("email"),
                    "outstandingMinor": 0, "openChargeCount": 0}
        customer_id, now = _uuid(), _iso(datetime.now(timezone.utc))
        item = {"PK": _business_key(business_id), "SK": f"CUSTOMER#{customer_id}", "entity": "customer", "id": customer_id,
                "business_id": business_id, "display_name": display_name, "email": email, "created_at": now}
        try:
            writes = [{"Put": {"TableName": self.table_name, "Item": self._marshal(item), "ConditionExpression": "attribute_not_exists(PK)"}}]
            if creation_key:
                writes.append({"Put": {"TableName": self.table_name, "Item": self._marshal({"PK": _business_key(business_id), "SK": f"CUSTOMER_KEY#{creation_key}", "entity": "customer_key", "customer_id": customer_id}), "ConditionExpression": "attribute_not_exists(PK)"}})
            self.client.transact_write_items(TransactItems=writes)
        except ClientError as error:
            if not self._conditional(error):
                raise
            # A concurrent creator with the same creation_key racing this
            # one always resolves by re-reading -- the `existing` check at
            # the top will find their committed row. Bounded so sustained
            # throttling backs off and eventually surfaces instead of
            # recursing without limit.
            if _attempt >= _MAX_CONFLICT_RETRIES:
                raise RuntimeError("customer creation conflicted repeatedly; retry with the same idempotency key") from error
            _backoff_sleep(_attempt)
            return self.create_customer(business_id, display_name, email, creation_key, _attempt=_attempt + 1)
        return {"customerId": customer_id, "displayName": display_name, "email": email,
                "outstandingMinor": 0, "openChargeCount": 0}

    def list_customers(self, business_id, limit=100):
        return self.customer_page(business_id, limit)["items"]

    def customer_page(self, business_id, limit=100):
        values = self._items(business_id, "CUSTOMER#")
        charges_by_customer = {}
        for charge in self._items(business_id, "CHARGE#"):
            if charge.get("cancelled_at") or int(charge["outstanding_minor"]) <= 0:
                continue
            current = charges_by_customer.setdefault(charge["customer_id"], {"outstandingMinor": 0, "openChargeCount": 0})
            current["outstandingMinor"] += int(charge["outstanding_minor"])
            current["openChargeCount"] += 1
        customers = [{"customerId": item["id"], "displayName": item["display_name"], "email": item.get("email"),
                      **charges_by_customer.get(item["id"], {"outstandingMinor": 0, "openChargeCount": 0})}
                     for item in sorted(values, key=lambda item: item["created_at"], reverse=True)]
        return _bounded_page(customers, limit)

    def business_summary(self, business_id):
        # Aggregated over every open charge via _items' full internal
        # pagination -- unlike list_charges' capped page for display, this
        # total must never quietly reflect only the most recent 100 charges.
        open_count, outstanding_minor = 0, 0
        for charge in self._items(business_id, "CHARGE#"):
            if charge.get("cancelled_at") or int(charge["outstanding_minor"]) <= 0:
                continue
            open_count += 1
            outstanding_minor += int(charge["outstanding_minor"])
        return {"openChargeCount": open_count, "outstandingMinor": outstanding_minor}

    def create_charge(self, business_id, customer_id, amount_minor, currency, description, due_date,
                      created_by_membership_id=None, creation_key=None, link_expires_at=None, link_token=None, *, _attempt=1):
        if not isinstance(amount_minor, int) or isinstance(amount_minor, bool) or amount_minor <= 0:
            raise ValueError("amount_minor must be a positive integer")
        if currency != "MXN":
            raise ValueError("the first slice supports MXN only")
        description = str(description).strip()
        if not description or len(description) > 240:
            raise ValueError("description must contain 1-240 characters")
        if self._get(business_id, f"CUSTOMER#{customer_id}") is None:
            raise ValueError("customer not found")
        token = link_token or secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        if creation_key:
            existing = self._get(business_id, f"CHARGE_KEY#{creation_key}")
            if existing:
                charge = self._get(business_id, f"CHARGE#{existing['charge_id']}")
                expected = (customer_id, amount_minor, currency, description, _iso(due_date))
                actual = (charge["customer_id"], int(charge["amount_minor"]), charge["currency"], charge["description"], charge["due_date"])
                if actual != expected:
                    raise ValueError("idempotency key was used with different charge data")
                return {"chargeId": charge["id"], "folio": charge["folio"], "token": token}
        business = self._get(business_id, "BUSINESS")
        if business is None:
            raise ValueError("business not found")
        counter = self._get(business_id, "FOLIO_COUNTER") or {"value": 0}
        sequence = int(counter["value"]) + 1
        charge_id, link_id, now = _uuid(), _uuid(), _iso(datetime.now(timezone.utc))
        folio = f"{business['folio_prefix']}-{sequence:06d}"
        pk = _business_key(business_id)
        charge = {"PK": pk, "SK": f"CHARGE#{charge_id}", "entity": "charge", "id": charge_id, "business_id": business_id,
                  "customer_id": customer_id, "folio": folio, "amount_minor": amount_minor, "allocated_minor": 0,
                  "outstanding_minor": amount_minor, "currency": currency, "description": description, "due_date": _iso(due_date),
                  "cancelled_at": None, "created_at": now, "creation_key": creation_key}
        link = {"PK": pk, "SK": f"LINK#{link_id}", "entity": "link", "id": link_id, "business_id": business_id, "charge_id": charge_id,
                "token_digest": digest, "expires_at": _iso(link_expires_at), "revoked_at": None, "created_at": now,
                "GSI1PK": f"LINK#{digest}", "GSI1SK": "LINK"}
        try:
            self.client.transact_write_items(TransactItems=[
                {"Update": {"TableName": self.table_name, "Key": {"PK": pk, "SK": "FOLIO_COUNTER"},
                            "UpdateExpression": "SET #value = :sequence", "ConditionExpression": "attribute_not_exists(#value) OR #value = :previous",
                            "ExpressionAttributeNames": {"#value": "value"}, "ExpressionAttributeValues": {":sequence": sequence, ":previous": sequence - 1}}},
                {"Put": {"TableName": self.table_name, "Item": self._marshal(charge), "ConditionExpression": "attribute_not_exists(PK)"}},
                {"Put": {"TableName": self.table_name, "Item": self._marshal(link), "ConditionExpression": "attribute_not_exists(PK)"}},
                {"Put": {"TableName": self.table_name, "Item": self._marshal({"PK": pk, "SK": f"CHARGE_KEY#{creation_key}", "entity": "charge_key", "charge_id": charge_id}) , "ConditionExpression": "attribute_not_exists(PK)"}} if creation_key else {"ConditionCheck": {"TableName": self.table_name, "Key": {"PK": pk, "SK": "BUSINESS"}, "ConditionExpression": "attribute_exists(PK)"}},
            ])
        except ClientError as error:
            if self._conditional(error) and creation_key:
                # A concurrent creator with the same creation_key always
                # resolves by re-reading -- the `existing` check at the top
                # will find their committed row. Bounded so sustained
                # throttling backs off and surfaces instead of recursing
                # without limit.
                if _attempt >= _MAX_CONFLICT_RETRIES:
                    raise RuntimeError("charge creation conflicted repeatedly; retry with the same idempotency key") from error
                _backoff_sleep(_attempt)
                return self.create_charge(business_id, customer_id, amount_minor, currency, description, due_date, created_by_membership_id, creation_key, link_expires_at, token, _attempt=_attempt + 1)
            if self._conditional(error):
                raise RuntimeError("charge creation conflicted; retry with the same idempotency key") from error
            raise
        return {"chargeId": charge_id, "folio": folio, "token": token}

    @staticmethod
    def _marshal(value):
        # boto3's DynamoDB client serializes native Python values.  Keeping this
        # helper makes transactional item construction explicit without a
        # second (and invalid) AttributeValue serialization pass.
        return {key: item for key, item in value.items() if item is not None}

    def find_charge_by_token_digest(self, digest, now):
        link = self._lookup(f"LINK#{bytes(digest).hex()}")
        if not link or link.get("revoked_at") or (link.get("expires_at") and link["expires_at"] <= _iso(now)):
            return None
        charge = self._get(link["business_id"], f"CHARGE#{link['charge_id']}")
        if not charge or charge.get("cancelled_at"):
            return None
        business = self._get(link["business_id"], "BUSINESS")
        branding = (business or {}).get("branding") or {}
        value = dict(charge)
        value["merchant_display_name"] = branding.get("publicName") or (business or {}).get("display_name")
        value["public_branding"] = {key: branding[key] for key in ("accent", "accentHover", "nav", "navAlt", "canvas") if key in branding} or None
        return self._charge(value)

    def charge_status(self, charge):
        if charge.outstanding_minor == 0:
            return "paid"
        adjustments = self._items(charge.business_id, f"ADJUSTMENT#")
        effective = [item for item in adjustments if item.get("charge_id") == charge.id and item.get("status") in ("approved", "confirmed", "completed")]
        if effective:
            kind = sorted(effective, key=lambda item: item.get("occurred_at") or "", reverse=True)[0]["kind"]
            return "reversed" if kind == "chargeback" else ("refunded" if charge.outstanding_minor == charge.amount_minor else "partially_refunded")
        return "partially_paid" if charge.outstanding_minor < charge.amount_minor else "pending"

    def active_connection(self, business_id, provider):
        values = [item for item in self._items(business_id, "CONNECTION#") if item["provider"] == provider and item.get("verified_at") and not item.get("disabled_at")]
        return self._connection(sorted(values, key=lambda item: item["verified_at"], reverse=True)[0]) if values else None

    def _connection_by_id(self, connection_id):
        return self._lookup(f"CONNECTION#{connection_id}")

    def connection_runtime_config(self, connection_id):
        item = self._connection_by_id(connection_id)
        if not item or item.get("disabled_at") or not item.get("verified_at"):
            return None
        return MerchantRuntimeConfig(self._connection(item), item["credential_secret_ref"], item["webhook_secret_ref"], item["credential_source"])

    def connection_verification_config(self, connection_id):
        item = self._connection_by_id(connection_id)
        if not item or item.get("disabled_at") or item.get("environment") != "test" or item.get("credential_source") != "test_credentials":
            return None
        return MerchantRuntimeConfig(self._connection(item), item["credential_secret_ref"], item["webhook_secret_ref"], item["credential_source"])

    def mark_connection_verified(self, connection_id, now):
        item = self._connection_by_id(connection_id)
        if not item or item.get("disabled_at") or item.get("environment") != "test" or item.get("credential_source") != "test_credentials":
            raise ValueError("sandbox merchant connection not found")
        self.table.update_item(Key={"PK": item["PK"], "SK": item["SK"]}, UpdateExpression="SET verified_at = :now", ExpressionAttributeValues={":now": _iso(now)})

    def get_or_create_attempt(self, charge, connection, submission_key, now):
        guard_sk = f"ACTIVE_ATTEMPT#{charge.id}"
        guard = self._get(charge.business_id, guard_sk)
        if guard:
            item = self._get(charge.business_id, f"ATTEMPT#{guard['attempt_id']}")
            if item and item["status"] in ("creating", "unknown", "ready", "expiring"):
                return self._attempt(item), False
        attempt_id, operation_key = _uuid(), _uuid()
        item = {"PK": _business_key(charge.business_id), "SK": f"ATTEMPT#{attempt_id}", "entity": "attempt", "id": attempt_id,
                "business_id": charge.business_id, "charge_id": charge.id, "merchant_connection_id": connection.id, "operation_key": operation_key,
                "submission_key": submission_key, "expected_amount_minor": charge.outstanding_minor, "currency": charge.currency,
                "provider": connection.provider, "environment": connection.environment, "status": "creating", "created_at": _iso(now), "updated_at": _iso(now),
                "GSI1PK": f"ATTEMPT#{attempt_id}", "GSI1SK": "ATTEMPT", "GSI3PK": "WORK#reconciliation", "GSI3SK": f"{_iso(now)}#{attempt_id}"}
        try:
            self.client.transact_write_items(TransactItems=[
                {"Put": {"TableName": self.table_name, "Item": self._marshal(item), "ConditionExpression": "attribute_not_exists(PK)"}},
                {"Put": {"TableName": self.table_name, "Item": self._marshal({"PK": _business_key(charge.business_id), "SK": guard_sk, "entity": "active_attempt", "attempt_id": attempt_id, "business_id": charge.business_id}), "ConditionExpression": "attribute_not_exists(PK)"}},
            ])
            return self._attempt(item), True
        except ClientError as error:
            if self._conditional(error):
                # The competing transaction may have committed its guard just
                # after this cancellation.  Reads are strongly consistent; a
                # bounded reread covers that handoff without creating another
                # attempt or spinning indefinitely.
                for _ in range(3):
                    guard = self._get(charge.business_id, guard_sk)
                    if guard:
                        existing = self._get(charge.business_id, f"ATTEMPT#{guard['attempt_id']}")
                        if existing:
                            return self._attempt(existing), False
                raise RuntimeError("active checkout attempt could not be resolved") from error
            raise

    def _attempt_by_id(self, attempt_id):
        return self._lookup(f"ATTEMPT#{attempt_id}")

    def mark_attempt_ready(self, business_id, attempt_id, checkout, now):
        # A direct PK/SK GET on the base table, not a LookupIndex (GSI)
        # query: every caller of this method already has the attempt's own
        # business_id in hand (PaymentAttempt carries it), typically because
        # it just created or fetched the attempt itself moments earlier in
        # the same request. The base table supports ConsistentRead; a GSI
        # never does, so resolving this by business_id sidesteps the GSI's
        # eventual-consistency window entirely instead of retrying through
        # it (R30's earlier, weaker mitigation).
        item = self._get(business_id, f"ATTEMPT#{attempt_id}")
        if not item or item["status"] not in ("creating", "unknown", "ready"):
            raise RuntimeError("attempt was completed with a different preference")
        if item["status"] == "ready" and item.get("provider_preference_id") != checkout.preference_id:
            raise RuntimeError("attempt was completed with a different preference")
        self.table.update_item(Key={"PK": item["PK"], "SK": item["SK"]}, UpdateExpression="SET #status=:status, provider_preference_id=:preference, checkout_url=:url, updated_at=:now", ExpressionAttributeNames={"#status": "status"}, ExpressionAttributeValues={":status": "ready", ":preference": checkout.preference_id, ":url": checkout.checkout_url, ":now": _iso(now)})
        item.update(status="ready", provider_preference_id=checkout.preference_id, checkout_url=checkout.checkout_url, updated_at=_iso(now))
        return self._attempt(item)

    def mark_attempt_unknown(self, business_id, attempt_id, now):
        # See mark_attempt_ready: a direct, strongly consistent PK/SK read,
        # not the GSI this used to retry through.
        item = self._get(business_id, f"ATTEMPT#{attempt_id}")
        if item and item["status"] == "creating":
            self.table.update_item(Key={"PK": item["PK"], "SK": item["SK"]}, UpdateExpression="SET #status=:status, uncertain_at=:now, updated_at=:now", ExpressionAttributeNames={"#status": "status"}, ExpressionAttributeValues={":status": "unknown", ":now": _iso(now)})

    def mark_attempt_expired(self, attempt_id, lease_token, now):
        # A claim never rewrites status (see _claim_work): the persisted row
        # stays "expiring" throughout the lease, exactly as
        # claim_reconciliation_attempts returned it. The safety check here is
        # the lease token, exactly like every other finish_*/mark_*_reconciled
        # method in this file.
        item = self._attempt_by_id(attempt_id)
        if not item:
            return
        self.client.transact_write_items(TransactItems=[
            # Terminal: "expired" is never claimable again, so this also
            # drops the attempt out of WorkIndex (GSI3PK/GSI3SK) instead of
            # leaving a dead entry there forever.
            {"Update": {"TableName": self.table_name, "Key": {"PK": item["PK"], "SK": item["SK"]}, "UpdateExpression": "SET #status=:status, updated_at=:now REMOVE provider_preference_id, checkout_url, lease_token, lease_expires_at, GSI3PK, GSI3SK", "ConditionExpression": "lease_token=:token", "ExpressionAttributeNames": {"#status": "status"}, "ExpressionAttributeValues": {":status": "expired", ":now": _iso(now), ":token": lease_token}}},
            {"Delete": {"TableName": self.table_name, "Key": {"PK": item["PK"], "SK": f"ACTIVE_ATTEMPT#{item['charge_id']}"}, "ConditionExpression": "attempt_id=:attempt", "ExpressionAttributeValues": {":attempt": attempt_id}}},
        ])

    def payment_context(self, external_reference):
        attempt = self._attempt_by_id(str(external_reference))
        if not attempt:
            return None
        connection = self._connection_by_id(attempt["merchant_connection_id"])
        charge = self._get(attempt["business_id"], f"CHARGE#{attempt['charge_id']}")
        if not connection or not charge:
            return None
        return self._attempt(attempt), self._connection(connection), self._charge(charge)

    def list_charges(self, business_id, limit=100):
        charges = self._items(business_id, "CHARGE#")
        customers = {item["id"]: item for item in self._items(business_id, "CUSTOMER#")}
        return [{
            "chargeId": item["id"], "customerId": item["customer_id"],
            "customerName": customers.get(item["customer_id"], {}).get("display_name", "Cliente"),
            "folio": item["folio"], "amountMinor": int(item["amount_minor"]),
            "allocatedMinor": int(item["allocated_minor"]), "outstandingMinor": int(item["outstanding_minor"]),
            "currency": item["currency"], "description": item["description"], "dueDate": item["due_date"],
            "cancelled": bool(item.get("cancelled_at")), "createdAt": item["created_at"],
        } for item in sorted(charges, key=lambda value: value["created_at"], reverse=True)[:min(max(int(limit), 1), 200)]]

    def business_branding(self, business_id):
        business = self._get(business_id, "BUSINESS")
        if not business:
            raise ValueError("business not found")
        defaults = {"accent": "#ed684c", "accentHover": "#d8563c", "nav": "#101931", "navAlt": "#172545", "canvas": "#f5f5f2"}
        branding = {**defaults, **(business.get("branding") or {})}
        return {"publicName": branding.get("publicName") or business["display_name"], **{key: branding[key] for key in defaults}}

    def merchant_connection_status(self, business_id):
        values = self._items(business_id, "CONNECTION#")
        if not values:
            return {"provider": "mercado_pago", "state": "needs_configuration", "environment": None, "credentialSource": None, "verifiedAt": None, "displayAccount": None}
        value = sorted(values, key=lambda item: item.get("verified_at") or item.get("updated_at", ""), reverse=True)[0]
        state = "verified_test" if value.get("environment") == "test" and value.get("verified_at") and not value.get("disabled_at") and value.get("credential_source") == "test_credentials" else "unavailable"
        account = str(value["provider_account_id"])
        return {"provider": value["provider"], "state": state, "environment": value["environment"], "credentialSource": value["credential_source"], "verifiedAt": value.get("verified_at"), "displayAccount": "••••" + account[-4:] if len(account) >= 4 else "Configurada"}

    def work_queue(self, business_id, today, limit=50, include_reviews=False):
        customers = {item["id"]: item for item in self._items(business_id, "CUSTOMER#")}
        open_charges = [item for item in self._items(business_id, "CHARGE#") if not item.get("cancelled_at") and int(item["outstanding_minor"]) > 0 and item["due_date"] <= _iso(today)]
        items = [{"kind": "overdue_charge" if item["due_date"] < _iso(today) else "due_today", "id": item["id"], "title": customers.get(item["customer_id"], {}).get("display_name", "Cliente"), "detail": f"{item['folio']} · {item['description']}", "amountMinor": int(item["outstanding_minor"]), "dueDate": item["due_date"]} for item in sorted(open_charges, key=lambda value: (value["due_date"], value["created_at"]))]
        if include_reviews:
            items.extend({"kind": "review", "id": value["id"], "title": "Revisión financiera requerida", "detail": value.get("review_reason") or "Requiere decisión de la propietaria."} for value in self.list_reviews(business_id, limit))
        return items[:min(max(int(limit), 1), 100)]

    def cancel_charge(self, business_id, charge_id, membership_id, operation_key, now):
        charge = self._get(business_id, f"CHARGE#{charge_id}")
        if not charge:
            raise ValueError("charge not found")
        pk, value = _business_key(business_id), _iso(now)
        audit = {"PK": pk, "SK": f"AUDIT#charge.cancelled#{operation_key}", "entity": "audit", "id": _uuid(), "business_id": business_id, "action": "charge.cancelled", "aggregate_id": charge_id, "operation_key": operation_key, "actor_id": membership_id, "occurred_at": value}
        links = [item for item in self._items(business_id, "LINK#") if item["charge_id"] == charge_id]
        writes = [
            {"Update": {"TableName": self.table_name, "Key": {"PK": pk, "SK": charge["SK"]}, "UpdateExpression": "SET cancelled_at=:now", "ExpressionAttributeValues": {":now": value}}},
            {"Put": {"TableName": self.table_name, "Item": self._marshal(audit), "ConditionExpression": "attribute_not_exists(PK)"}},
        ]
        writes.extend({"Update": {"TableName": self.table_name, "Key": {"PK": pk, "SK": link["SK"]}, "UpdateExpression": "SET revoked_at=:now", "ExpressionAttributeValues": {":now": value}}} for link in links)
        try:
            self.client.transact_write_items(TransactItems=writes)
        except ClientError as error:
            if not self._conditional(error):
                raise
        return {"chargeId": charge_id, "folio": charge["folio"], "cancelled": True}

    def charge_detail(self, business_id, charge_id):
        charge = self._get(business_id, f"CHARGE#{charge_id}")
        if not charge:
            return None
        customer = self._get(business_id, f"CUSTOMER#{charge['customer_id']}")
        links = [item for item in self._items(business_id, "LINK#") if item["charge_id"] == charge_id]
        link = sorted(links, key=lambda item: item["created_at"], reverse=True)[0] if links else None
        attempts = [item for item in self._items(business_id, "ATTEMPT#") if item["charge_id"] == charge_id]
        payments = [item for item in self._items(business_id, "PAYMENT#") if item.get("charge_id") == charge_id]
        allocations = [item for item in self._items(business_id, "ALLOCATION#") if item["charge_id"] == charge_id]
        adjustments = [item for item in self._items(business_id, "ADJUSTMENT#") if item.get("charge_id") == charge_id]
        payment_ids = {item["id"] for item in payments}
        provider_payment_ids = {item["provider_payment_id"] for item in payments}
        events = [item for item in self._items(business_id, "EVENT#") if item["provider_resource_id"] in provider_payment_ids]
        refunds = [item for item in self._items(business_id, "REFUND#") if item["payment_id"] in payment_ids]
        return {"charge": {"chargeId": charge["id"], "customerId": charge["customer_id"], "folio": charge["folio"], "amountMinor": int(charge["amount_minor"]), "allocatedMinor": int(charge["allocated_minor"]), "outstandingMinor": int(charge["outstanding_minor"]), "currency": charge["currency"], "description": charge["description"], "dueDate": charge["due_date"], "cancelled": bool(charge.get("cancelled_at")), "createdAt": charge["created_at"], "customerName": (customer or {}).get("display_name", "Cliente")}, "customer": {"customerId": charge["customer_id"], "displayName": (customer or {}).get("display_name", "Cliente"), "email": (customer or {}).get("email")}, "paymentLink": {"state": "available_once" if link and not link.get("revoked_at") else "unavailable", "issuedAt": link.get("created_at") if link and not link.get("revoked_at") else None}, "attempts": [{"attemptId": item["id"], "status": item["status"], "expectedAmountMinor": int(item["expected_amount_minor"]), "createdAt": item["created_at"], "updatedAt": item["updated_at"]} for item in sorted(attempts, key=lambda value: value["created_at"], reverse=True)], "payments": [{"paymentId": item["id"], "providerPaymentId": item["provider_payment_id"], "providerStatus": item["provider_status"], "amountMinor": int(item["amount_minor"]), "currency": item["currency"], "environment": item["environment"], "providerLiveMode": item.get("provider_live_mode"), "observedAt": item["provider_observed_at"], "approvedAt": item.get("approved_at"), "reviewReason": item.get("review_reason")} for item in sorted(payments, key=lambda value: value["provider_observed_at"], reverse=True)], "allocations": [{"allocationId": item["id"], "paymentId": item["payment_id"], "amountMinor": int(item["amount_minor"]), "createdAt": item["created_at"]} for item in allocations], "adjustments": [{"adjustmentId": item["id"], "paymentId": item["payment_id"], "allocationId": item.get("allocation_id"), "kind": item["kind"], "status": item["status"], "amountMinor": int(item["amount_minor"]), "effectAppliedMinor": int(item.get("effect_applied_minor") or 0), "effectAppliedAt": item.get("effect_applied_at"), "occurredAt": item.get("occurred_at")} for item in adjustments], "providerEvents": [{"eventId": item["id"], "eventType": item["event_type"], "processingStatus": item["processing_status"], "receivedAt": item["received_at"], "reason": item.get("processing_error")} for item in sorted(events, key=lambda value: value["received_at"], reverse=True)], "refundOperations": [{"refundId": item["id"], "paymentId": item["payment_id"], "amountMinor": int(item["amount_minor"]), "status": item["status"], "providerRefundId": item.get("provider_refund_id"), "updatedAt": item["updated_at"], "lastError": item.get("last_error")} for item in sorted(refunds, key=lambda value: value["updated_at"], reverse=True)]}

    @staticmethod
    def _event_sk(connection_id, event_key):
        return "EVENT#" + hashlib.sha256(f"{connection_id}\n{event_key}".encode()).hexdigest()

    def capture_provider_event(self, connection_id, event_key, resource_id, event_type, payload, signature_valid, now):
        connection = self._connection_by_id(connection_id)
        if not connection:
            raise ValueError("connection not found")
        sk = self._event_sk(connection_id, event_key)
        existing = self._get(connection["business_id"], sk)
        if existing:
            return {"id": existing["id"], "status": existing["processing_status"]}
        event_id, value = _uuid(), _iso(now)
        item = {"PK": _business_key(connection["business_id"]), "SK": sk, "entity": "provider_event", "id": event_id, "business_id": connection["business_id"], "merchant_connection_id": connection_id, "provider": connection["provider"], "environment": connection["environment"], "provider_event_key": event_key, "provider_resource_id": resource_id, "event_type": event_type, "signature_valid": bool(signature_valid), "raw_payload": payload, "received_at": value, "processing_status": "accepted", "available_at": value, "GSI1PK": f"EVENT#{event_id}", "GSI1SK": "EVENT", "GSI3PK": "WORK#provider_event", "GSI3SK": f"{value}#{event_id}"}
        try:
            self._put(item, condition="attribute_not_exists(PK)")
            return {"id": event_id, "status": "accepted"}
        except ClientError as error:
            if not self._conditional(error):
                raise
            existing = self._get(connection["business_id"], sk)
            return {"id": existing["id"], "status": existing["processing_status"]}

    def _event(self, connection_id, event_key):
        connection = self._connection_by_id(connection_id)
        return self._get(connection["business_id"], self._event_sk(connection_id, event_key)) if connection else None

    def _set_event(self, connection_id, event_key, status, now, reason=None):
        event = self._event(connection_id, event_key)
        if not event:
            return
        # "processed" (done) and "review" (paused) both leave
        # claim_provider_events' claimable set ("accepted","failed",
        # "processing"), so both drop the WorkIndex entry too;
        # resolve_review's "retry" action restores it for "review". "failed"
        # stays claimable and keeps its entry untouched.
        removes = ", GSI3PK, GSI3SK" if status in ("processed", "review") else ""
        self.table.update_item(Key={"PK": event["PK"], "SK": event["SK"]}, UpdateExpression=f"SET processing_status=:status, processing_error=:reason, processed_at=:now REMOVE lease_token, lease_expires_at{removes}", ExpressionAttributeValues={":status": status, ":reason": str(reason)[:500] if reason else None, ":now": _iso(now)})

    def mark_provider_event_failed(self, connection_id, event_key, error, now):
        self._set_event(connection_id, event_key, "failed", now, error)

    def mark_provider_event_review(self, connection_id, event_key, reason, now):
        self._set_event(connection_id, event_key, "review", now, reason)

    def record_payment_observation(self, context, payment, assessment, event_key, now):
        if context:
            attempt, connection, charge = context
        else:
            attempt = charge = None
            connection_item = self._connection_by_id(self.webhook_connection_id) if self.webhook_connection_id else None
            if not connection_item:
                raise RuntimeError("unmatched payment requires its webhook connection")
            connection = self._connection(connection_item)
        business_id, pk = connection.business_id, _business_key(connection.business_id)
        identity = hashlib.sha256(f"mercado_pago\n{connection.environment}\n{payment.provider_account_id}\n{payment.provider_payment_id}".encode()).hexdigest()
        owner_key = {"PK": f"PAYMENT_IDENTITY#{identity}", "SK": "OWNER"}
        owner = self.table.get_item(Key=owner_key, ConsistentRead=True).get("Item")
        if owner:
            if owner["business_id"] != business_id:
                raise RuntimeError("provider payment is already owned by another business")
            stored = self._get(business_id, f"PAYMENT#{owner['payment_id']}")
            incoming_at = _iso(payment.provider_updated_at or payment.observed_at)
            stored_at = (stored or {}).get("provider_updated_at") or (stored or {}).get("provider_observed_at")
            # Provider snapshots are ordered by provider time, not webhook
            # arrival.  A late event is evidence only and cannot regress money.
            if stored and stored_at and incoming_at < stored_at:
                if self.webhook_connection_id:
                    self._set_event(self.webhook_connection_id, event_key, "processed", now)
                return replace(assessment, review_reason=None, allocation_minor=0)
            self._apply_existing_payment_snapshot(stored, payment, assessment, now)
            if self.webhook_connection_id:
                self._set_event(self.webhook_connection_id, event_key, "review" if assessment.review_reason else "processed", now, assessment.review_reason)
            return replace(assessment, review_reason=None, allocation_minor=0)
        payment_id, observed = _uuid(), _iso(payment.observed_at)
        allocation_minor = min(int(payment.amount_minor), charge.outstanding_minor) if assessment.allocate and charge else 0
        review_reason = assessment.review_reason or ("extra_payment" if assessment.allocate and allocation_minor < payment.amount_minor else None)
        payment_item = {"PK": pk, "SK": f"PAYMENT#{payment_id}", "entity": "payment", "id": payment_id, "business_id": business_id, "charge_id": charge.id if charge else None, "payment_attempt_id": attempt.id if attempt else None, "merchant_connection_id": connection.id, "provider": "mercado_pago", "environment": connection.environment, "provider_account_id": payment.provider_account_id, "provider_payment_id": payment.provider_payment_id, "provider_status": payment.status, "amount_minor": int(payment.amount_minor), "currency": payment.currency, "approved_at": _iso(payment.approved_at), "provider_observed_at": observed, "provider_updated_at": _iso(payment.provider_updated_at), "provider_live_mode": payment.provider_live_mode, "review_reason": review_reason, "created_at": _iso(now), "updated_at": _iso(now), "GSI1PK": f"PAYMENT#{payment_id}", "GSI1SK": "PAYMENT"}
        owner_item = {**owner_key, "entity": "payment_identity", "business_id": business_id, "payment_id": payment_id}
        writes = [{"Put": {"TableName": self.table_name, "Item": self._marshal(owner_item), "ConditionExpression": "attribute_not_exists(PK)"}}, {"Put": {"TableName": self.table_name, "Item": self._marshal(payment_item), "ConditionExpression": "attribute_not_exists(PK)"}}]
        if allocation_minor:
            allocation_id = _uuid()
            allocation = {"PK": pk, "SK": f"ALLOCATION#{allocation_id}", "entity": "allocation", "id": allocation_id, "business_id": business_id, "payment_id": payment_id, "charge_id": charge.id, "amount_minor": allocation_minor, "created_at": _iso(now)}
            writes.extend([{"Update": {"TableName": self.table_name, "Key": {"PK": pk, "SK": f"CHARGE#{charge.id}"}, "UpdateExpression": "SET allocated_minor = allocated_minor + :amount, outstanding_minor = outstanding_minor - :amount", "ConditionExpression": "outstanding_minor = :expected AND attribute_not_exists(cancelled_at)", "ExpressionAttributeValues": {":amount": allocation_minor, ":expected": charge.outstanding_minor}}}, {"Put": {"TableName": self.table_name, "Item": self._marshal(allocation), "ConditionExpression": "attribute_not_exists(PK)"}}])
        audit = {"PK": pk, "SK": f"AUDIT#payment.observed#{payment.provider_payment_id}", "entity": "audit", "id": _uuid(), "business_id": business_id, "action": "payment.observed", "aggregate_id": payment_id, "operation_key": payment.provider_payment_id, "actor_id": connection.id, "occurred_at": _iso(now), "details": {"reviewReason": review_reason, "allocationMinor": allocation_minor}}
        writes.append({"Put": {"TableName": self.table_name, "Item": self._marshal(audit), "ConditionExpression": "attribute_not_exists(PK)"}})
        topic = "payment_review" if review_reason else ("payment_approved" if allocation_minor else "payment_observed")
        outbox_id = _uuid()
        outbox = {"PK": pk, "SK": f"OUTBOX#{topic}#{payment.provider_payment_id}", "entity": "outbox", "id": outbox_id, "business_id": business_id, "topic": topic, "operation_key": payment.provider_payment_id, "payload": {"paymentId": payment_id, "chargeId": charge.id if charge else None, "connectionId": connection.id, "environment": payment.environment}, "status": "pending", "available_at": _iso(now), "attempt_count": 0, "GSI1PK": f"OUTBOX#{outbox_id}", "GSI1SK": "OUTBOX", "GSI3PK": "WORK#outbox", "GSI3SK": f"{_iso(now)}#{payment_id}"}
        writes.append({"Put": {"TableName": self.table_name, "Item": self._marshal(outbox), "ConditionExpression": "attribute_not_exists(PK)"}})
        try:
            self.client.transact_write_items(TransactItems=writes)
        except ClientError as error:
            if not self._conditional(error):
                raise
            claimed = self.table.get_item(Key=owner_key, ConsistentRead=True).get("Item")
            if claimed:
                if claimed["business_id"] != business_id:
                    raise RuntimeError("provider payment is already owned by another business")
                return replace(assessment, review_reason=None, allocation_minor=0)
            if allocation_minor and charge is not None:
                fresh_charge = self._get(business_id, f"CHARGE#{charge.id}")
                if fresh_charge and fresh_charge.get("cancelled_at"):
                    # The charge was cancelled concurrently with this
                    # payment's confirmation -- the whole transaction above
                    # rolled back, so nothing was persisted yet. That
                    # condition can never pass again (the charge stays
                    # cancelled), so retrying the same allocation forever
                    # would only ever fail forever. Record the payment
                    # immediately, without allocation, as a financial review
                    # instead of losing the evidence or looping on it.
                    return self._record_cancelled_charge_payment(business_id, pk, charge, connection, attempt, payment, payment_id, observed, owner_item, event_key, now, assessment)
            # A different financial write changed the charge after we read it.
            # Never create a second payment identity or loop indefinitely: the
            # worker must fetch fresh provider/charge context on its next lease.
            raise RuntimeError("payment observation conflicted with a newer charge balance") from error
        if self.webhook_connection_id:
            self._set_event(self.webhook_connection_id, event_key, "review" if review_reason else "processed", now, review_reason)
        return replace(assessment, review_reason=review_reason, allocation_minor=allocation_minor)

    def _record_cancelled_charge_payment(self, business_id, pk, charge, connection, attempt, payment, payment_id, observed, owner_item, event_key, now, assessment):
        """Persist real payment evidence for a charge that turned out to be
        cancelled, without allocating it. Called only after the allocating
        transaction in record_payment_observation rolled back specifically
        because of a concurrent cancellation; owner_item/payment_id/observed
        are the exact ones from that attempt, so this simply omits the
        allocation + charge-balance writes and reroutes to review."""
        review_reason = "charge_cancelled_after_payment"
        payment_item = {"PK": pk, "SK": f"PAYMENT#{payment_id}", "entity": "payment", "id": payment_id, "business_id": business_id, "charge_id": charge.id, "payment_attempt_id": attempt.id if attempt else None, "merchant_connection_id": connection.id, "provider": "mercado_pago", "environment": connection.environment, "provider_account_id": payment.provider_account_id, "provider_payment_id": payment.provider_payment_id, "provider_status": payment.status, "amount_minor": int(payment.amount_minor), "currency": payment.currency, "approved_at": _iso(payment.approved_at), "provider_observed_at": observed, "provider_updated_at": _iso(payment.provider_updated_at), "provider_live_mode": payment.provider_live_mode, "review_reason": review_reason, "created_at": _iso(now), "updated_at": _iso(now), "GSI1PK": f"PAYMENT#{payment_id}", "GSI1SK": "PAYMENT"}
        audit = {"PK": pk, "SK": f"AUDIT#payment.observed#{payment.provider_payment_id}", "entity": "audit", "id": _uuid(), "business_id": business_id, "action": "payment.observed", "aggregate_id": payment_id, "operation_key": payment.provider_payment_id, "actor_id": connection.id, "occurred_at": _iso(now), "details": {"reviewReason": review_reason, "allocationMinor": 0}}
        outbox_id = _uuid()
        outbox = {"PK": pk, "SK": f"OUTBOX#payment_review#{payment.provider_payment_id}", "entity": "outbox", "id": outbox_id, "business_id": business_id, "topic": "payment_review", "operation_key": payment.provider_payment_id, "payload": {"paymentId": payment_id, "chargeId": charge.id, "connectionId": connection.id, "environment": payment.environment}, "status": "pending", "available_at": _iso(now), "attempt_count": 0, "GSI1PK": f"OUTBOX#{outbox_id}", "GSI1SK": "OUTBOX", "GSI3PK": "WORK#outbox", "GSI3SK": f"{_iso(now)}#{payment_id}"}
        self.client.transact_write_items(TransactItems=[
            {"Put": {"TableName": self.table_name, "Item": self._marshal(owner_item), "ConditionExpression": "attribute_not_exists(PK)"}},
            {"Put": {"TableName": self.table_name, "Item": self._marshal(payment_item), "ConditionExpression": "attribute_not_exists(PK)"}},
            {"Put": {"TableName": self.table_name, "Item": self._marshal(audit), "ConditionExpression": "attribute_not_exists(PK)"}},
            {"Put": {"TableName": self.table_name, "Item": self._marshal(outbox), "ConditionExpression": "attribute_not_exists(PK)"}},
        ])
        if self.webhook_connection_id:
            self._set_event(self.webhook_connection_id, event_key, "review", now, review_reason)
        return replace(assessment, allocate=False, review_reason=review_reason, allocation_minor=0)

    def _apply_existing_payment_snapshot(self, stored, payment, assessment, now):
        """Apply a later provider snapshot without duplicating its payment.

        A refund/reversal is an immutable adjustment and a compensating
        balance mutation.  It is never a deletion of the original
        allocation.  A specific provider_adjustment_id is *not* immutable,
        though, and neither is its status: the same adjustment id can be
        observed again with a different status (pending/rejected -> approved
        as the provider's own view of it settles, or -- with no safe
        automatic handling -- approved -> rejected, and back again) as
        postgres.py's reference ON CONFLICT ... DO UPDATE already treats it.

        `status` is therefore *not* the source of truth for whether money has
        moved: it is free to keep evolving (approved -> rejected -> approved
        -> ...) for as long as the provider keeps changing its mind. Whether
        the compensating balance mutation has actually happened is tracked
        by a separate, one-way marker -- effect_applied_at/
        effect_applied_minor -- that starts unset and, once set by a
        transition into an effective status, is never reset and never
        recomputed by a later observation, no matter how many more times the
        reported status swings between effective and non-effective. Without
        this, approved -> rejected -> approved would restore the same money
        twice (rejected reads as "not effective", so the second approval
        looks like a fresh transition).
        """
        if not stored:
            raise RuntimeError("payment identity has no payment record")
        business_id, pk = stored["business_id"], stored["PK"]
        allocations = [item for item in self._items(business_id, "ALLOCATION#") if item["payment_id"] == stored["id"]]
        allocation = allocations[0] if allocations else None
        charge = self._get(business_id, f"CHARGE#{allocation['charge_id']}") if allocation else None
        adjustment_specs = list(payment.adjustments)
        if assessment.review_reason and allocation:
            adjustment_specs.append(type("Correction", (), {"provider_adjustment_id": "validation-correction", "kind": "correction", "amount_minor": int(allocation["amount_minor"]), "status": "confirmed", "occurred_at": now})())
        incoming_at = _iso(payment.provider_updated_at or payment.observed_at)
        contradiction_reason = None
        # Cap every restore against the Charge's *current* allocated_minor,
        # not just this allocation's original, never-updated amount: an
        # earlier call may already have restored part of it (via a different
        # adjustment, or an earlier observation of this one), and
        # allocated_minor is the one counter that reflects that.
        remaining_allocation = int(charge["allocated_minor"]) if charge else 0
        total_restore = 0
        # A review already open for this payment (from an earlier
        # observation's contradiction, or an earlier assessment) is a
        # standing, human-owned flag: resolve_review's "acknowledge" action is
        # the only thing that clears it. An unrelated later observation that
        # finds nothing new wrong must not silently clear it just because
        # *this* call's own assessment happens to be clean.
        stored_review_reason = stored.get("review_reason")
        writes = [{"Update": {"TableName": self.table_name, "Key": {"PK": stored["PK"], "SK": stored["SK"]}, "UpdateExpression": "SET provider_status=:status, provider_observed_at=:observed, provider_updated_at=:updated, provider_live_mode=:live, review_reason=:review, updated_at=:now", "ExpressionAttributeValues": {":status": payment.status, ":observed": _iso(payment.observed_at), ":updated": _iso(payment.provider_updated_at), ":live": payment.provider_live_mode, ":review": assessment.review_reason or stored_review_reason, ":now": _iso(now)}}}]
        for adjustment in adjustment_specs:
            key = hashlib.sha256(str(adjustment.provider_adjustment_id).encode()).hexdigest()
            sk = f"ADJUSTMENT#{stored['id']}#{key}"
            existing = self._get(business_id, sk)
            now_effective = adjustment.status in ("approved", "confirmed", "completed")
            if existing is None:
                # Reported and applied amounts are tracked separately from
                # the moment an adjustment id is first seen: amount_minor is
                # the provider's own face value (may be corrected later by a
                # fresh observation of the same id); effect_applied_minor is
                # what this specific transition actually moved, capped by
                # what was left to restore, and is set at most once.
                apply_amount = min(int(adjustment.amount_minor), remaining_allocation) if (now_effective and allocation) else 0
                item = {
                    "PK": pk, "SK": sk, "entity": "adjustment", "id": _uuid(), "business_id": business_id,
                    "payment_id": stored["id"], "allocation_id": allocation["id"] if allocation else None,
                    "charge_id": allocation["charge_id"] if allocation else None,
                    "provider_adjustment_id": adjustment.provider_adjustment_id, "kind": adjustment.kind,
                    "status": adjustment.status, "amount_minor": int(adjustment.amount_minor),
                    "effect_applied_at": _iso(now) if now_effective else None, "effect_applied_minor": apply_amount,
                    "occurred_at": _iso(adjustment.occurred_at), "provider_observed_at": _iso(payment.observed_at),
                    "provider_updated_at": _iso(payment.provider_updated_at), "created_at": _iso(now),
                }
                writes.append({"Put": {"TableName": self.table_name, "Item": self._marshal(item), "ConditionExpression": "attribute_not_exists(PK)"}})
                if now_effective and allocation and apply_amount:
                    remaining_allocation -= apply_amount
                    total_restore += apply_amount
                continue
            existing_at = existing.get("provider_updated_at") or existing.get("provider_observed_at")
            if existing_at and incoming_at < existing_at:
                # Strictly older evidence for an adjustment id we've already
                # seen newer evidence for -- never rewrites it.
                continue
            already_applied = bool(existing.get("effect_applied_at"))
            values = {
                ":status": adjustment.status, ":amount": int(adjustment.amount_minor), ":occurred": _iso(adjustment.occurred_at),
                ":observed": _iso(payment.observed_at), ":updated": _iso(payment.provider_updated_at), ":now": _iso(now),
                ":expected_status": existing["status"], ":expected_observed": existing.get("provider_observed_at"),
            }
            set_clause = "#status=:status, amount_minor=:amount, occurred_at=:occurred, provider_observed_at=:observed, provider_updated_at=:updated, updated_at=:now"
            # Compare-and-swap on the exact state this decision was based on:
            # if a second worker raced this same transition and already
            # committed, its write changed status and/or provider_observed_at
            # (and, for the not-yet-applied case, effect_applied_at itself),
            # so this one is rejected instead of also restoring the same
            # money or clobbering a newer report.
            condition = "#status=:expected_status AND provider_observed_at=:expected_observed"
            if already_applied:
                # The financial effect for this id is locked in for good --
                # effect_applied_at/effect_applied_minor are never touched
                # again, whether this observation repeats the same effective
                # status, corrects the reported amount, or contradicts it
                # ("approved" -> "rejected"). There is no safe automatic rule
                # for the contradiction -- see the pilot readiness audit for
                # why -- so the already-confirmed effect is retained and the
                # contradiction is flagged for manual review instead of
                # guessed at. A *later* reversal back to effective (rejected
                # -> approved again) is exactly as much a no-op for money as
                # a duplicate approved: effect_applied_at already exists.
                if not now_effective:
                    contradiction_reason = "adjustment_effective_reversed"
                condition += " AND attribute_exists(effect_applied_at)"
            elif now_effective:
                # The first transition into an effective status for this id:
                # apply the compensating balance change now, and latch
                # effect_applied_at so no future observation of this same id
                # -- including one that first sees it revert to non-effective
                # and only later sees "approved" again -- can ever apply it a
                # second time.
                apply_amount = min(int(adjustment.amount_minor), remaining_allocation) if allocation else 0
                values[":applied_at"], values[":applied_amount"] = _iso(now), apply_amount
                set_clause += ", effect_applied_at=:applied_at, effect_applied_minor=:applied_amount"
                condition += " AND attribute_not_exists(effect_applied_at)"
                if allocation and apply_amount:
                    remaining_allocation -= apply_amount
                    total_restore += apply_amount
            # else: still not effective (e.g. pending -> rejected without
            # ever having been approved) -- only the reported fields above
            # change; there is no financial effect to guard.
            writes.append({"Update": {
                "TableName": self.table_name,
                "Key": {"PK": pk, "SK": sk},
                "UpdateExpression": f"SET {set_clause}",
                "ConditionExpression": condition,
                "ExpressionAttributeNames": {"#status": "status"},
                "ExpressionAttributeValues": values,
            }})
        if total_restore and allocation and charge:
            writes.append({"Update": {"TableName": self.table_name, "Key": {"PK": pk, "SK": charge["SK"]}, "UpdateExpression": "SET allocated_minor=allocated_minor - :amount, outstanding_minor=outstanding_minor + :amount", "ConditionExpression": "allocated_minor >= :amount", "ExpressionAttributeValues": {":amount": total_restore}}})
            # A refund/reversal/chargeback reopened outstanding balance on the
            # charge.  The Checkout Pro preference tied to the payment_attempt
            # that produced this payment now targets a stale amount and must
            # never be resurfaced to the payer.  Flip it to "expiring" in the
            # same transaction as the balance restore (mirrors the historical
            # PostgreSQL reference in payments/postgres.py); the reconciliation
            # worker's existing expiring->expired transition then releases the
            # ACTIVE_ATTEMPT#{chargeId} guard so the next checkout request
            # creates a fresh attempt instead of reusing this one. A late
            # provider response for this attempt cannot revive it either:
            # mark_attempt_ready only accepts "creating"/"unknown"/"ready".
            attempt_id = stored.get("payment_attempt_id")
            if attempt_id:
                attempt = self._get(business_id, f"ATTEMPT#{attempt_id}")
                if attempt and attempt["status"] == "ready":
                    writes.append({"Update": {"TableName": self.table_name, "Key": {"PK": attempt["PK"], "SK": attempt["SK"]}, "UpdateExpression": "SET #status=:expiring, updated_at=:now REMOVE lease_token, lease_expires_at", "ConditionExpression": "#status=:ready", "ExpressionAttributeNames": {"#status": "status"}, "ExpressionAttributeValues": {":expiring": "expiring", ":ready": "ready", ":now": _iso(now)}}})
        if contradiction_reason:
            # Surface the contradiction on the payment's own review_reason,
            # so it appears in list_reviews without inventing a separate
            # review entity just for this. A contradiction found in this
            # very call is the freshest possible signal, so it wins over
            # both this call's own (unrelated) assessment and whatever was
            # already stored.
            writes[0]["Update"]["ExpressionAttributeValues"][":review"] = contradiction_reason
        try:
            self.client.transact_write_items(TransactItems=writes)
        except ClientError as error:
            if self._conditional(error):
                raise RuntimeError("payment adjustment conflicted with a newer balance") from error
            raise

    def list_reviews(self, business_id, limit=100):
        return self.review_page(business_id, limit)["items"]

    def review_page(self, business_id, limit=100):
        values = [item for item in self._items(business_id, "PAYMENT#") if item.get("review_reason")]
        payments = [{"kind": "payment", "id": item["id"], "providerPaymentId": item["provider_payment_id"], "status": item["provider_status"], "amountMinor": int(item["amount_minor"]), "currency": item["currency"], "reason": item["review_reason"], "updatedAt": item["updated_at"]} for item in values]
        refunds = [{"kind": "refund", "id": item["id"], "paymentId": item["payment_id"], "status": item["status"], "amountMinor": int(item["amount_minor"]), "reason": item.get("last_error"), "updatedAt": item["updated_at"]} for item in self._items(business_id, "REFUND#") if item["status"] == "review"]
        events = [{"kind": "provider_event", "id": item["id"], "providerEventKey": item["provider_event_key"], "providerResourceId": item["provider_resource_id"], "eventType": item["event_type"], "reason": item.get("processing_error"), "updatedAt": item["received_at"]} for item in self._items(business_id, "EVENT#") if item.get("processing_status") == "review"]
        reviews = sorted(payments + refunds + events, key=lambda item: item["updatedAt"], reverse=True)
        return _bounded_page(reviews, limit)

    def create_refund_operation(self, business_id, payment_id, membership_id, operation_key, amount_minor, now, *, _attempt=1):
        payment = self._by_id("payment", payment_id)
        if not payment or payment["business_id"] != business_id:
            raise ValueError("payment not found")
        if payment.get("review_reason") not in (None, "extra_payment") or payment["provider_status"] not in ("approved", "refunded"):
            raise ValueError("payment is not refundable")
        key = hashlib.sha256(f"{payment_id}\n{operation_key}".encode()).hexdigest()
        sk = f"REFUND#{payment_id}#{key}"
        existing = self._get(business_id, sk)
        if existing:
            same = (amount_minor is None and existing["full_refund"]) or (amount_minor is not None and not existing["full_refund"] and int(existing["amount_minor"]) == amount_minor)
            if not same:
                raise ValueError("idempotency key was used with a different refund amount")
            return {"refundId": existing["id"], "amountMinor": int(existing["amount_minor"]), "status": existing["status"], "providerRefundId": existing.get("provider_refund_id")}
        lock_sk = f"REFUND_LOCK#{payment_id}"
        if self._get(business_id, lock_sk):
            # Fast path only: a plain read cannot by itself prevent two
            # concurrent requests with different Idempotency-Key values from
            # both passing this check and both trying to create a refund for
            # the same payment. The REFUND_LOCK#{paymentId} item created
            # below, transactionally alongside the refund itself, is what
            # actually makes "at most one unresolved refund per payment" a
            # guarantee rather than a race -- exactly one caller can ever
            # win attribute_not_exists(PK) on that one item.
            raise ValueError("another refund operation is unresolved")
        adjusted = sum(int(item["amount_minor"]) for item in self._items(business_id, "ADJUSTMENT#") if item["payment_id"] == payment_id and item["status"] in ("approved", "confirmed", "completed"))
        refundable = int(payment["amount_minor"]) - adjusted
        requested = refundable if amount_minor is None else amount_minor
        if not isinstance(requested, int) or isinstance(requested, bool) or requested <= 0:
            raise ValueError("refund amount must be a positive integer")
        if requested > refundable:
            raise ValueError("refund amount exceeds refundable balance")
        refund_id, value = _uuid(), _iso(now)
        item = {"PK": _business_key(business_id), "SK": sk, "entity": "refund", "id": refund_id, "business_id": business_id, "payment_id": payment_id, "merchant_connection_id": payment["merchant_connection_id"], "requested_by_membership_id": membership_id, "operation_key": operation_key, "amount_minor": requested, "full_refund": amount_minor is None, "status": "requested", "available_at": value, "attempt_count": 0, "updated_at": value, "GSI1PK": f"REFUND#{refund_id}", "GSI1SK": "REFUND", "GSI3PK": "WORK#refund", "GSI3SK": f"{value}#{refund_id}"}
        lock = {"PK": _business_key(business_id), "SK": lock_sk, "entity": "refund_lock", "business_id": business_id, "payment_id": payment_id, "refund_id": refund_id, "created_at": value}
        audit = {"PK": item["PK"], "SK": f"AUDIT#refund.requested#{operation_key}", "entity": "audit", "id": _uuid(), "business_id": business_id, "action": "refund.requested", "aggregate_id": refund_id, "operation_key": operation_key, "actor_id": membership_id, "occurred_at": value, "details": {"paymentId": payment_id, "amountMinor": requested}}
        try:
            self.client.transact_write_items(TransactItems=[
                {"Put": {"TableName": self.table_name, "Item": self._marshal(item), "ConditionExpression": "attribute_not_exists(PK)"}},
                {"Put": {"TableName": self.table_name, "Item": self._marshal(lock), "ConditionExpression": "attribute_not_exists(PK)"}},
                {"Put": {"TableName": self.table_name, "Item": self._marshal(audit), "ConditionExpression": "attribute_not_exists(PK)"}},
            ])
        except ClientError as error:
            if not self._conditional(error):
                raise
            # Re-read authoritative state instead of trusting which
            # CancellationReasons index failed: when two concurrent requests
            # share the SAME payment_id *and* Idempotency-Key, this
            # request's own refund row (index 0) can fail its
            # attribute_not_exists(PK) condition too -- the other caller's
            # identical operation_key already won it -- at the same time as
            # the shared REFUND_LOCK (index 1). Checking only the lock's
            # reason would misreport that exact case as "another refund
            # operation is unresolved" when it is really this same request,
            # already satisfied by the winner. A strongly consistent re-read
            # of this operation's own row is the only way to tell the two
            # apart.
            existing = self._get(business_id, sk)
            if existing:
                same = (amount_minor is None and existing["full_refund"]) or (amount_minor is not None and not existing["full_refund"] and int(existing["amount_minor"]) == amount_minor)
                if not same:
                    raise ValueError("idempotency key was used with a different refund amount") from error
                return {"refundId": existing["id"], "amountMinor": int(existing["amount_minor"]), "status": existing["status"], "providerRefundId": existing.get("provider_refund_id")}
            # This request's own row was never written -- the conflict was on
            # something else. If the lock belongs to a genuinely different,
            # still-unresolved refund for this payment, no amount of
            # retrying this operation_key changes that outcome.
            if self._get(business_id, lock_sk):
                raise ValueError("another refund operation is unresolved") from error
            # Neither this operation's own row nor the lock exist: a
            # transient conflict (e.g. throttling, or a race that resolved
            # itself before this re-read) -- bounded retry, same as every
            # other conflict path in this file.
            if _attempt >= _MAX_CONFLICT_RETRIES:
                raise RuntimeError("refund operation creation conflicted repeatedly; retry with the same idempotency key") from error
            _backoff_sleep(_attempt)
            return self.create_refund_operation(business_id, payment_id, membership_id, operation_key, amount_minor, now, _attempt=_attempt + 1)
        return {"refundId": refund_id, "amountMinor": requested, "status": "requested", "providerRefundId": None}

    def claim_refund_operations(self, now, lease_expires_at, limit=10):
        values = self._claim_work("refund", now, lease_expires_at, limit, "refund", ("requested", "processing", "unknown", "accepted", "failed"), "attempt_count")
        for value in values:
            value.setdefault("provider_refund_id", None)
            # table.query (boto3 resource) always deserializes DynamoDB's
            # Number type as Decimal, never int, regardless of what was
            # written. Every other read path in this file (_attempt, _charge,
            # etc.) casts its numeric fields the same way before handing them
            # back; this one previously didn't, and a real partial refund
            # (an explicit amount_minor) would fail _provider_number's
            # isinstance(int) check every time against the real repository.
            value["amount_minor"] = int(value["amount_minor"])
        return values

    def refund_payment_reference(self, business_id, payment_id):
        payment = self._by_id("payment", payment_id)
        return payment["provider_payment_id"] if payment and payment["business_id"] == business_id else None

    def refund_reconciled(self, refund_id, lease_token, provider_refund_id):
        refund = self._by_id("refund", refund_id)
        return bool(refund and refund.get("lease_token") == lease_token and any(item["payment_id"] == refund["payment_id"] and item.get("provider_adjustment_id") == provider_refund_id and item["kind"] == "refund" and int(item["amount_minor"]) == int(refund["amount_minor"]) and item["status"] in ("approved", "confirmed", "completed") for item in self._items(refund["business_id"], "ADJUSTMENT#")))

    def finish_refund_operation(self, refund_id, lease_token, status, now, provider_refund_id=None, error=None, available_at=None):
        if status not in ("unknown", "accepted", "completed", "failed", "review"):
            raise ValueError("invalid refund operation status")
        refund = self._by_id("refund", refund_id)
        if not refund:
            return
        # "completed" (done) and "review" (paused for an owner decision) are
        # both not claimable -- claim_refund_operations' statuses tuple is
        # ("requested","processing","unknown","accepted","failed") -- so both
        # drop out of WorkIndex too. resolve_review's "retry" action restores
        # it for the "review" case; "unknown"/"accepted"/"failed" stay
        # claimable and keep their WorkIndex entry untouched.
        removes = ", GSI3PK, GSI3SK" if status in ("completed", "review") else ""
        update_kwargs = {
            "Key": {"PK": refund["PK"], "SK": refund["SK"]},
            "UpdateExpression": "SET #status=:status, provider_refund_id=:provider, last_error=:error, available_at=:available, updated_at=:now REMOVE lease_token, lease_expires_at" + removes,
            "ConditionExpression": "lease_token=:token",
            "ExpressionAttributeNames": {"#status": "status"},
            "ExpressionAttributeValues": {":status": status, ":provider": provider_refund_id or refund.get("provider_refund_id"), ":error": str(error)[:500] if error else None, ":available": _iso(available_at) if available_at else refund["available_at"], ":now": _iso(now), ":token": lease_token},
        }
        if status != "completed":
            self.table.update_item(**update_kwargs)
            return
        # Terminal: release the payment's REFUND_LOCK atomically with the
        # status transition, so a fresh refund request for the same payment
        # becomes possible again in the same instant it is safe to allow one.
        self.client.transact_write_items(TransactItems=[
            {"Update": {"TableName": self.table_name, **update_kwargs}},
            {"Delete": {"TableName": self.table_name, "Key": {"PK": refund["PK"], "SK": f"REFUND_LOCK#{refund['payment_id']}"}, "ConditionExpression": "refund_id=:refund", "ExpressionAttributeValues": {":refund": refund_id}}},
        ])

    def resolve_review(self, business_id, review_kind, review_id, membership_id, operation_key, action, note, now):
        if review_kind not in ("payment", "refund", "provider_event") or action not in ("retry", "acknowledge"):
            raise ValueError("invalid review action")
        note = str(note or "").strip()
        if not note or len(note) > 500:
            raise ValueError("review note must contain 1-500 characters")
        resolution_sk = f"REVIEW_RESOLUTION#{operation_key}"
        existing = self._get(business_id, resolution_sk)
        if existing:
            return {"kind": review_kind, "reviewId": str(review_id), "action": action, "outcome": existing["outcome"]}
        entity = {"payment": "payment", "refund": "refund", "provider_event": "provider_event"}[review_kind]
        target = self._by_id(entity, review_id)
        if not target or target["business_id"] != business_id:
            raise ValueError("open review not found")
        # A "review" refund/provider_event had its WorkIndex entry
        # (GSI3PK/GSI3SK) removed by finish_refund_operation/_set_event when
        # it entered review -- it isn't claimable while paused, so it
        # shouldn't clutter the index either. "retry" must restore that
        # entry (using now, not the item's original creation time, so it is
        # queued as of when it actually became eligible again) or the
        # retried operation would never be found by a future claim.
        # "acknowledge" removes it again defensively; finish_refund_operation
        # already did, but REMOVE on an absent attribute is a harmless no-op.
        if review_kind == "payment":
            if not target.get("review_reason"):
                raise ValueError("open payment review not found")
            updates, outcome = ("SET updated_at=:now REMOVE review_reason", "acknowledged_unallocated") if action == "acknowledge" else ("SET review_retry_requested_at=:now, updated_at=:now", "queued")
        elif review_kind == "refund":
            if target["status"] != "review":
                raise ValueError("open refund review not found")
            updates, outcome = ("SET #status=:status, available_at=:now, updated_at=:now, GSI3PK=:gsi3pk, GSI3SK=:gsi3sk REMOVE lease_token, lease_expires_at", "queued") if action == "retry" else ("SET #status=:status, updated_at=:now REMOVE lease_token, lease_expires_at, GSI3PK, GSI3SK", "acknowledged")
        else:
            if target.get("processing_status") != "review":
                raise ValueError("open provider event review not found")
            updates, outcome = ("SET processing_status=:status, available_at=:now, GSI3PK=:gsi3pk, GSI3SK=:gsi3sk REMOVE lease_token, lease_expires_at", "queued") if action == "retry" else ("SET processing_status=:status, processed_at=:now REMOVE lease_token, lease_expires_at, GSI3PK, GSI3SK", "acknowledged")
        values = {":now": _iso(now)}
        names = {}
        if review_kind == "refund":
            names["#status"] = "status"; values[":status"] = "requested" if action == "retry" else "resolved"
            if action == "retry":
                values[":gsi3pk"] = "WORK#refund"; values[":gsi3sk"] = f"{_iso(now)}#{review_id}"
        elif review_kind == "provider_event":
            values[":status"] = "accepted" if action == "retry" else "processed"
            if action == "retry":
                values[":gsi3pk"] = "WORK#provider_event"; values[":gsi3sk"] = f"{_iso(now)}#{review_id}"
        update_args = {"Key": {"PK": target["PK"], "SK": target["SK"]}, "UpdateExpression": updates, "ExpressionAttributeValues": values}
        if names:
            update_args["ExpressionAttributeNames"] = names
        resolution = {"PK": _business_key(business_id), "SK": resolution_sk, "entity": "review_resolution", "id": _uuid(), "business_id": business_id, "review_kind": review_kind, "review_id": str(review_id), "action": action, "note": note, "resolved_by_membership_id": membership_id, "operation_key": operation_key, "outcome": outcome, "created_at": _iso(now)}
        if review_kind == "refund" and action == "acknowledge":
            # Terminal (status -> "resolved"): release the payment's
            # REFUND_LOCK in the same transaction as the acknowledgement, so
            # a fresh refund request becomes possible again in the same
            # instant it is safe to allow one -- matching the release on the
            # worker's own "completed" path in finish_refund_operation.
            self.client.transact_write_items(TransactItems=[
                {"Update": {"TableName": self.table_name, **update_args}},
                {"Put": {"TableName": self.table_name, "Item": self._marshal(resolution), "ConditionExpression": "attribute_not_exists(PK)"}},
                {"Delete": {"TableName": self.table_name, "Key": {"PK": target["PK"], "SK": f"REFUND_LOCK#{target['payment_id']}"}, "ConditionExpression": "refund_id=:refund", "ExpressionAttributeValues": {":refund": str(review_id)}}},
            ])
        else:
            self.table.update_item(**update_args)
            self._put(resolution, condition="attribute_not_exists(PK)")
        return {"kind": review_kind, "reviewId": str(review_id), "action": action, "outcome": outcome}

    @staticmethod
    def _import_job_view(item, progress=None):
        summary = item["summary"]
        return {
            "importId": item["id"], "businessId": item["business_id"], "status": item["status"],
            "source": dict(item["source"]), "profile": dict(item["profile"]),
            "summary": {
                "inputRows": int(summary["inputRows"]), "validRows": int(summary["validRows"]),
                "errorRows": int(summary["errorRows"]), "totalMinor": int(summary["totalMinor"]),
            },
            "issues": [{"row": int(issue["row"]), "field": issue["field"], "message": issue["message"]} for issue in item.get("issues", [])],
            "issuesTruncated": bool(item.get("issues_truncated")),
            "createdAt": item["created_at"], "validatedAt": item.get("validated_at"),
            "createdBySubject": item["created_by_subject"],
            # When DynamoDB's TTL will remove the temporary row chunks (the
            # only personal data of this job). Informational: the server
            # itself never serves a chunk past this instant (see
            # load_import_rows), whether or not TTL has physically run.
            "rowsExpireAt": item.get("rows_expire_at"),
            # Server-derived apply progress (None until an apply starts). The
            # only evidence the UI may treat as "applied": never a client-side
            # guess. See DynamoRepository._apply_progress.
            "apply": progress,
        }

    def create_import_job(self, business_id, subject_id, idempotency_key, payload_digest, status,
                           source, profile, summary, issues_sample, issues_truncated, row_chunks, now, *,
                           rows_expires_at, _attempt=1):
        """Persist a validated/invalid import job transactionally, exactly
        once per (business, subject, idempotency_key, payload).

        Deliberately does NOT reuse LookupIndex (the GSI every other
        `_by_id`-style lookup in this file goes through): a GSI is never
        eligible for ConsistentRead, and a staff member re-reading the job
        they just created (e.g. the Auditoría tab) moments later must never
        see a transient miss. IMPORT_LOOKUP#{importId} is a second, tiny
        item under the same strongly-consistent business partition instead,
        resolved with the same ConsistentRead GetItem as everything else in
        this file -- one extra small item, in exchange for never depending
        on eventual consistency for a read that follows a write by moments.

        No secondary lock item either (unlike refunds): two DIFFERENT
        idempotency keys creating two import jobs concurrently is not a
        conflict to prevent -- they are legitimately two different jobs.
        Only the SAME key racing itself must resolve to one job, which the
        guard item's own attribute_not_exists(PK) condition already
        guarantees.

        The audit item's SK is keyed by `guard_key` (the sha256 of
        `subject_id + idempotency_key`, the exact same value the guard
        item's own SK uses), never by `idempotency_key` alone: two
        different superadmins can legitimately submit the same
        Idempotency-Key value in the same business (they have no way to
        coordinate with each other on it), and since every import for a
        business shares one partition, an SK keyed only on the raw key
        would collide between them and fail the whole transaction for the
        second subject. The action embeds `status` (`import.validated` or
        `import.invalid`) so the audit trail never claims a batch that
        failed validation was accepted.
        """
        guard_key = hashlib.sha256(f"{subject_id}\n{idempotency_key}".encode()).hexdigest()
        guard_sk = f"IMPORT_KEY#{guard_key}"
        existing_guard = self._get(business_id, guard_sk)
        if existing_guard:
            if existing_guard["payload_digest"] != payload_digest:
                raise ImportIdempotencyConflict("idempotency key was used with a different import payload")
            metadata = self._get(business_id, existing_guard["metadata_sk"])
            if not metadata:
                raise RuntimeError("import job record is missing for an existing idempotency key")
            return self._import_job_view(metadata)

        if self._get(business_id, "BUSINESS") is None:
            raise ValueError("business not found")

        import_id, value = _uuid(), _iso(now)
        pk = _business_key(business_id)
        metadata_sk = f"IMPORT#{value}#{import_id}"
        metadata_item = {
            "PK": pk, "SK": metadata_sk, "entity": "import_job", "id": import_id, "business_id": business_id,
            "status": status, "source": source, "profile": profile, "summary": summary,
            "issues": issues_sample, "issues_truncated": issues_truncated,
            "created_at": value, "validated_at": value, "created_by_subject": subject_id,
            "rows_expire_at": _iso(rows_expires_at), "guard_sk": guard_sk,
        }
        guard_item = {
            "PK": pk, "SK": guard_sk, "entity": "import_key", "business_id": business_id,
            "import_id": import_id, "payload_digest": payload_digest, "metadata_sk": metadata_sk, "created_at": value,
        }
        lookup_item = {"PK": pk, "SK": f"IMPORT_LOOKUP#{import_id}", "entity": "import_lookup", "business_id": business_id, "metadata_sk": metadata_sk}
        audit_item = {
            "PK": pk, "SK": f"AUDIT#import.{status}#{guard_key}", "entity": "audit", "id": _uuid(), "business_id": business_id,
            "action": f"import.{status}", "aggregate_id": import_id, "operation_key": idempotency_key, "actor_id": subject_id,
            "occurred_at": value, "details": {"status": status, **summary},
        }
        writes = [
            {"Put": {"TableName": self.table_name, "Item": self._marshal(guard_item), "ConditionExpression": "attribute_not_exists(PK)"}},
            {"Put": {"TableName": self.table_name, "Item": self._marshal(metadata_item), "ConditionExpression": "attribute_not_exists(PK)"}},
            {"Put": {"TableName": self.table_name, "Item": self._marshal(lookup_item), "ConditionExpression": "attribute_not_exists(PK)"}},
            {"Put": {"TableName": self.table_name, "Item": self._marshal(audit_item), "ConditionExpression": "attribute_not_exists(PK)"}},
        ]
        for index, chunk in enumerate(row_chunks):
            writes.append({"Put": {"TableName": self.table_name, "ConditionExpression": "attribute_not_exists(PK)", "Item": self._marshal({
                "PK": pk, "SK": f"IMPORT_ROWS#{import_id}#{index:04d}", "entity": "import_rows",
                "business_id": business_id, "import_id": import_id, "chunk_index": index, "rows": chunk,
                # DynamoDB TTL attribute (epoch seconds, a Number). Present
                # ONLY on these chunks -- the one item type that holds the
                # customers' personal data. Metadata, guards, lookup and
                # audit carry no PII and are deliberately not expired.
                "ttl": int(rows_expires_at.timestamp()),
            })}})
        # Defensive, not expected to ever trigger: imports.MAX_RECORDS_PER_IMPORT
        # and imports.ROWS_PER_CHUNK are chosen so this transaction always
        # stays far under DynamoDB's 100-item TransactWriteItems ceiling.
        if len(writes) > 100:
            raise RuntimeError("import batch produced too many transactional items")
        try:
            self.client.transact_write_items(TransactItems=writes)
        except ClientError as error:
            if not self._conditional(error):
                raise
            existing_guard = self._get(business_id, guard_sk)
            if existing_guard:
                if existing_guard["payload_digest"] != payload_digest:
                    raise ImportIdempotencyConflict("idempotency key was used with a different import payload") from error
                metadata = self._get(business_id, existing_guard["metadata_sk"])
                if metadata:
                    return self._import_job_view(metadata)
            if _attempt >= _MAX_CONFLICT_RETRIES:
                raise RuntimeError("import job creation conflicted repeatedly; retry with the same idempotency key") from error
            _backoff_sleep(_attempt)
            return self.create_import_job(business_id, subject_id, idempotency_key, payload_digest, status, source,
                                          profile, summary, issues_sample, issues_truncated, row_chunks, now,
                                          rows_expires_at=rows_expires_at, _attempt=_attempt + 1)
        return self._import_job_view(metadata_item)

    def _import_metadata(self, business_id, import_id):
        """The job's metadata item, strongly consistent, or None. Always
        resolved inside `business_id`'s own partition, so an import id that
        belongs to another business is indistinguishable from an unknown one."""
        lookup = self._get(business_id, f"IMPORT_LOOKUP#{import_id}")
        if not lookup:
            return None
        return self._get(business_id, lookup["metadata_sk"])

    def import_job_detail(self, business_id, import_id, now=None):
        metadata = self._import_metadata(business_id, import_id)
        if not metadata:
            # A purged job has no metadata by design; its audit evidence is
            # what lets it read as "purged" instead of "never existed".
            return self._purged_import_view(business_id, import_id)
        return self._apply_view(business_id, import_id, metadata, now or datetime.now(timezone.utc))

    def _apply_view(self, business_id, import_id, metadata, now):
        """The job as the API returns it, with apply progress when the job
        has one. Always built from a fresh read so a response is server
        evidence, never a cached or client-assembled state."""
        metadata = self._import_metadata(business_id, import_id) if metadata is None else metadata
        if metadata is None:
            return self._purged_import_view(business_id, import_id)
        progress = None
        if metadata["status"] in ("applying", "applied", "review"):
            progress = self._apply_progress(business_id, import_id, metadata, now)
        return self._import_job_view(metadata, progress)

    def _apply_progress(self, business_id, import_id, metadata, now):
        """Progress derived from the persisted per-row results (one item per
        decided row, written in the same transaction as the row's own
        Customer/Charge) -- a single source of truth, so a counter can never
        drift from what was actually written."""
        results = self._items(business_id, f"IMPORT_APPLYROW#{import_id}#")

        def count(field, value):
            return sum(1 for item in results if item.get(field) == value)

        conflicts = sorted((item for item in results if item.get("outcome") == "conflict"), key=lambda item: item["SK"])
        return {
            "rowsTotal": int(metadata["summary"]["inputRows"]), "rowsProcessed": len(results),
            "customersCreated": count("customer_action", "created"), "customersReused": count("customer_action", "reused"),
            "chargesCreated": count("charge_action", "created"), "chargesReused": count("charge_action", "reused"),
            "conflicts": len(conflicts), "failed": 1 if metadata.get("apply_last_error") else 0,
            "leaseActive": metadata["status"] == "applying" and metadata.get("apply_lease_expires_at", "") >= _iso(now),
            "lastErrorCode": metadata.get("apply_last_error"),
            "startedAt": metadata.get("apply_started_at"), "completedAt": metadata.get("apply_completed_at"),
            # Row number + fixed reason code only: enough to find the row in
            # the operator's own file, never any personal data.
            "conflictSample": [{"sourceRow": int(item["source_row"]), "code": item["reason"]} for item in conflicts[:imports_module.MAX_CONFLICT_SAMPLE]],
            "conflictsTruncated": len(conflicts) > imports_module.MAX_CONFLICT_SAMPLE,
        }

    def load_import_rows(self, business_id, import_id, now):
        """The job's stored row results, in order, or None when they are not
        (or no longer) available: chunks missing, incomplete, or past their
        TTL. A chunk whose `ttl` has passed is treated as gone even if
        DynamoDB has not physically deleted it yet (TTL deletion can lag by
        ~48h), so expired personal data is never served. Query on the
        business partition + prefix -- never a Scan. DynamoDB returns numbers
        as Decimal; they are converted back to int here so the money path
        never sees a non-int."""
        metadata = self._import_metadata(business_id, import_id)
        if not metadata:
            return None
        expected = -(-int(metadata["summary"]["inputRows"]) // imports_module.ROWS_PER_CHUNK)
        chunks = self._items(business_id, f"IMPORT_ROWS#{import_id}#")
        if len(chunks) != expected:
            return None
        cutoff = int(now.timestamp())
        ordered = sorted(chunks, key=lambda chunk: int(chunk["chunk_index"]))
        if [int(chunk["chunk_index"]) for chunk in ordered] != list(range(expected)):
            return None
        if any("ttl" in chunk and int(chunk["ttl"]) <= cutoff for chunk in ordered):
            return None
        return [_plain(chunk["rows"]) for chunk in ordered]

    _PURGEABLE_STATUSES = ("validated", "invalid", "applied", "review", "applying")
    # DynamoDB allows 100 operations / 4MB per TransactWriteItems. A purge
    # batch is at most this many key-only Deletes plus one metadata Update
    # that records how many were removed -- comfortably below both limits.
    _PURGE_DELETE_BATCH = 90

    def _purged_import_view(self, business_id, import_id):
        evidence = self._get(business_id, f"AUDIT#import.purged#{import_id}")
        if not evidence:
            return None
        details = evidence.get("details") or {}
        return {
            "importId": import_id, "businessId": business_id, "status": "purged", "purgedAt": evidence["occurred_at"],
            "purge": {
                "priorStatus": details.get("priorStatus"), "chunksDeleted": int(details.get("chunksDeleted", 0)),
                "rowResultsDeleted": int(details.get("rowResultsDeleted", 0)), "applyGuardsDeleted": int(details.get("applyGuardsDeleted", 0)),
            },
        }

    def _keys_under(self, business_id, prefix):
        """Just the keys under `prefix` in this business's partition (Query,
        strongly consistent, paginated -- never a Scan)."""
        from boto3.dynamodb.conditions import Key
        query = {
            "KeyConditionExpression": Key("PK").eq(_business_key(business_id)) & Key("SK").begins_with(prefix),
            "ConsistentRead": True, "ProjectionExpression": "#pk, #sk",
            "ExpressionAttributeNames": {"#pk": "PK", "#sk": "SK"},
        }
        keys = []
        while True:
            response = self.table.query(**query)
            keys.extend({"PK": item["PK"], "SK": item["SK"]} for item in response.get("Items", []))
            if not response.get("LastEvaluatedKey"):
                return keys
            query["ExclusiveStartKey"] = response["LastEvaluatedKey"]

    def purge_import_job(self, business_id, import_id, subject_id, idempotency_key, now):
        """Delete one ImportJob's temporary data and keep only PII-free audit
        evidence. Idempotent, resumable, and never runs against a live apply.

        Order matters for "no orphans": (1) fence -- flip the metadata to
        `purging` (refused while an apply holds a live lease; this also stops
        any apply from starting or continuing); (2) delete the children
        (row chunks, apply results, apply guards) in key-only transactions of
        <= 90 Deletes, each also recording its count on the metadata; (3) one
        final transaction writes the `import.purged` audit item and deletes
        the metadata, lookup and validate-guard together. Children go before
        the parent and the parent leaves atomically with its lookup, so no
        child ever outlives its metadata. A crash between steps leaves the job
        visibly `purging`; calling purge again resumes it.
        """
        pk = _business_key(business_id)
        for _ in range(3):
            metadata = self._import_metadata(business_id, import_id)
            if metadata is None:
                return self._purged_import_view(business_id, import_id)
            status = metadata["status"]
            if status == "purging":
                break
            if status not in self._PURGEABLE_STATUSES:
                raise ImportOperationRefused("import_not_purgeable", status)
            if status == "applying" and metadata.get("apply_lease_expires_at", "") >= _iso(now):
                raise ImportOperationRefused("import_apply_active", status)
            try:
                self.table.update_item(
                    Key={"PK": metadata["PK"], "SK": metadata["SK"]},
                    UpdateExpression="SET #s = :purging, purge_prior_status = :prior, purge_started_at = :now, purge_requested_by = :actor REMOVE apply_lease_token, apply_lease_expires_at",
                    ConditionExpression="#s = :prior AND (#s <> :applying OR attribute_not_exists(apply_lease_expires_at) OR apply_lease_expires_at < :now)",
                    ExpressionAttributeNames={"#s": "status"},
                    ExpressionAttributeValues={":purging": "purging", ":prior": status, ":applying": "applying", ":now": _iso(now), ":actor": subject_id},
                )
                break
            except ClientError as error:
                if not self._conditional(error):
                    raise
                # State moved under us (another purge, or an apply took the
                # lease): re-read and decide again instead of guessing.
        else:
            raise RuntimeError("import purge conflicted repeatedly; retry")

        import_sk_prefix = {"purge_chunks": f"IMPORT_ROWS#{import_id}#", "purge_results": f"IMPORT_APPLYROW#{import_id}#", "purge_guards": f"IMPORT_APPLY_KEY#{import_id}#"}
        for counter, prefix in import_sk_prefix.items():
            keys = self._keys_under(business_id, prefix)
            for start in range(0, len(keys), self._PURGE_DELETE_BATCH):
                batch = keys[start:start + self._PURGE_DELETE_BATCH]
                writes = [{"Delete": {"TableName": self.table_name, "Key": key}} for key in batch]
                writes.append({"Update": {
                    "TableName": self.table_name, "Key": {"PK": metadata["PK"], "SK": metadata["SK"]},
                    "UpdateExpression": "ADD #c :n", "ConditionExpression": "#s = :purging",
                    "ExpressionAttributeNames": {"#c": counter, "#s": "status"},
                    "ExpressionAttributeValues": {":n": len(batch), ":purging": "purging"},
                }})
                self.client.transact_write_items(TransactItems=writes)

        metadata = self._import_metadata(business_id, import_id)
        if metadata is None:
            return self._purged_import_view(business_id, import_id)
        details = {
            "priorStatus": metadata.get("purge_prior_status"),
            "chunksDeleted": int(metadata.get("purge_chunks", 0)),
            "rowResultsDeleted": int(metadata.get("purge_results", 0)),
            "applyGuardsDeleted": int(metadata.get("purge_guards", 0)),
        }
        audit = {
            "PK": pk, "SK": f"AUDIT#import.purged#{import_id}", "entity": "audit", "id": _uuid(), "business_id": business_id,
            "action": "import.purged", "aggregate_id": import_id, "operation_key": idempotency_key,
            "actor_id": metadata.get("purge_requested_by") or subject_id, "occurred_at": _iso(now), "details": details,
        }
        finals = [
            {"Put": {"TableName": self.table_name, "Item": self._marshal(audit), "ConditionExpression": "attribute_not_exists(PK)"}},
            {"Delete": {"TableName": self.table_name, "Key": {"PK": metadata["PK"], "SK": metadata["SK"]}, "ConditionExpression": "#s = :purging",
                        "ExpressionAttributeNames": {"#s": "status"}, "ExpressionAttributeValues": {":purging": "purging"}}},
            {"Delete": {"TableName": self.table_name, "Key": {"PK": pk, "SK": f"IMPORT_LOOKUP#{import_id}"}}},
        ]
        if metadata.get("guard_sk"):
            finals.append({"Delete": {"TableName": self.table_name, "Key": {"PK": pk, "SK": metadata["guard_sk"]}}})
        try:
            self.client.transact_write_items(TransactItems=finals)
        except ClientError as error:
            if not self._conditional(error):
                raise
            # A concurrent purge finished first (its audit already exists).
            view = self._purged_import_view(business_id, import_id)
            if view is None:
                raise RuntimeError("import purge conflicted; retry") from error
            return view
        return self._purged_import_view(business_id, import_id)

    def list_import_jobs(self, business_id, limit, cursor=None):
        from boto3.dynamodb.conditions import Key
        query = {
            "KeyConditionExpression": Key("PK").eq(_business_key(business_id)) & Key("SK").begins_with("IMPORT#"),
            "ScanIndexForward": False,
            "Limit": min(max(int(limit), 1), 100),
        }
        if cursor:
            query["ExclusiveStartKey"] = _decode_import_cursor(cursor, business_id)
        response = self.table.query(**query)
        items = [self._import_job_view(item) for item in response.get("Items", [])]
        last_key = response.get("LastEvaluatedKey")
        return {"items": items, "hasMore": bool(last_key), "cursor": _encode_import_cursor(last_key) if last_key else None}

    # ---- Apply -------------------------------------------------------------
    #
    # validated -> applying -> applied | review. One transaction per row
    # (<= 5 items), processed in bounded slices; see
    # docs/delivery/2026-09-19-etl-retention-and-apply.md for the design.

    _APPLY_ROW_MAX_ATTEMPTS = 5

    def _load_apply_rows(self, business_id, import_id, metadata, now):
        """The stored rows, freshly read and re-validated server-side, or a
        refusal. Runs before anything is written, so an unusable batch never
        even enters `applying`."""
        status = metadata["status"]
        try:
            chunks = self.load_import_rows(business_id, import_id, now)
        except ValueError as error:  # a stored number that is not a whole int
            raise ImportOperationRefused("import_data_invalid", status) from error
        if chunks is None:
            raise ImportOperationRefused("import_rows_unavailable", status)
        flat = [row for chunk in chunks for row in chunk]
        if not imports_module.revalidate_stored_rows(flat, _plain(metadata["summary"])):
            raise ImportOperationRefused("import_data_invalid", status)
        return chunks

    def _acquire_apply_lease(self, business_id, import_id, subject_id, idempotency_key, guard_sk, clock, lease_seconds):
        """Take the job's apply lease with one conditional transaction.
        Returns ("leased", token), ("busy", None) when another invocation
        holds a live lease, or ("finished", None) when the job completed
        meanwhile. Also registers this Idempotency-Key's guard and, on the
        first start, the `import.apply_started` audit -- atomically."""
        token = _uuid()
        pk = _business_key(business_id)
        for _ in range(3):
            now = clock()
            metadata = self._import_metadata(business_id, import_id)
            if metadata is None:
                raise ImportOperationRefused("import_not_applicable", "purged")
            status = metadata["status"]
            has_guard = self._get(business_id, guard_sk) is not None
            if status in ("applied", "review"):
                if has_guard:
                    return "finished", None
                raise ImportOperationRefused("import_not_applicable", status)
            if status not in ("validated", "applying"):
                raise ImportOperationRefused("import_not_applicable", status)
            if status == "applying" and metadata.get("apply_lease_expires_at", "") >= _iso(now):
                return "busy", None
            writes = [{"Update": {
                "TableName": self.table_name, "Key": {"PK": metadata["PK"], "SK": metadata["SK"]},
                "UpdateExpression": "SET #s = :applying, apply_lease_token = :token, apply_lease_expires_at = :expires, "
                                    "apply_started_at = if_not_exists(apply_started_at, :now), apply_started_by = if_not_exists(apply_started_by, :actor) "
                                    "REMOVE apply_last_error",
                "ConditionExpression": "#s = :prior AND (#s = :validated OR attribute_not_exists(apply_lease_expires_at) OR apply_lease_expires_at < :now)",
                "ExpressionAttributeNames": {"#s": "status"},
                "ExpressionAttributeValues": {
                    ":applying": "applying", ":prior": status, ":validated": "validated", ":token": token,
                    ":expires": _iso(now + timedelta(seconds=lease_seconds)), ":now": _iso(now), ":actor": subject_id,
                },
            }}]
            if not has_guard:
                guard = {"PK": pk, "SK": guard_sk, "entity": "import_apply_key", "business_id": business_id, "import_id": import_id, "created_at": _iso(now)}
                writes.append({"Put": {"TableName": self.table_name, "Item": self._marshal(guard), "ConditionExpression": "attribute_not_exists(PK)"}})
            if status == "validated":
                audit = {
                    "PK": pk, "SK": f"AUDIT#import.apply_started#{import_id}", "entity": "audit", "id": _uuid(), "business_id": business_id,
                    "action": "import.apply_started", "aggregate_id": import_id, "operation_key": idempotency_key, "actor_id": subject_id,
                    "occurred_at": _iso(now), "details": {"rows": int(metadata["summary"]["inputRows"])},
                }
                writes.append({"Put": {"TableName": self.table_name, "Item": self._marshal(audit), "ConditionExpression": "attribute_not_exists(PK)"}})
            try:
                self.client.transact_write_items(TransactItems=writes)
                return "leased", token
            except ClientError as error:
                if not self._conditional(error):
                    raise
                # State moved (another invocation leased/finished, or a purge
                # fenced the job): re-read and decide again.
        raise RuntimeError("import apply could not take its lease; retry with the same idempotency key")

    def _apply_import_row(self, business_id, import_id, business, metadata, token, clock, chunk_index, position, row):
        """Apply exactly one row in one transaction of <= 5 items:

        fence (metadata Update, only while WE hold the lease) + result item
        (attribute_not_exists: a row is decided once) + Customer Put (only if
        new) + folio counter Update and Charge Put (only if new). Because
        Customer/Charge/result share one transaction, "row applied" and "row
        recorded" can never disagree, and because their keys are
        deterministic a retry, a second job or a racing worker cannot create
        a second copy: it loses a conditional Put and re-reads.
        """
        pk = _business_key(business_id)
        record = row["normalized"]
        customer, charge = record["customer"], record["charge"]
        customer_id = imports_module.customer_id_for(business_id, customer["externalId"])
        charge_id = imports_module.charge_id_for(business_id, charge["externalId"])
        result_sk = f"IMPORT_APPLYROW#{import_id}#{chunk_index:04d}#{position:02d}"
        for attempt in range(1, self._APPLY_ROW_MAX_ATTEMPTS + 1):
            existing_customer = self._get(business_id, f"CUSTOMER#{customer_id}")
            existing_charge = self._get(business_id, f"CHARGE#{charge_id}")
            decision = imports_module.decide_row(record, existing_customer, existing_charge, customer_id)
            now = _iso(clock())
            conflict = decision["conflict"]
            result = {
                "PK": pk, "SK": result_sk, "entity": "import_row_result", "business_id": business_id, "import_id": import_id,
                "chunk_index": chunk_index, "position": position, "source_row": record["sourceRow"],
                "outcome": "conflict" if conflict else ("created" if decision["createCharge"] else "reused"),
                "customer_action": "none" if conflict else ("created" if decision["createCustomer"] else "reused"),
                "charge_action": "none" if conflict else ("created" if decision["createCharge"] else "reused"),
                "customer_id": None if conflict else customer_id, "charge_id": None if conflict else charge_id,
                "reason": conflict, "applied_at": now,
            }
            writes = [
                {"Update": {
                    "TableName": self.table_name, "Key": {"PK": metadata["PK"], "SK": metadata["SK"]},
                    "UpdateExpression": "SET apply_heartbeat_at = :now", "ConditionExpression": "apply_lease_token = :token AND #s = :applying",
                    "ExpressionAttributeNames": {"#s": "status"},
                    "ExpressionAttributeValues": {":now": now, ":token": token, ":applying": "applying"},
                }},
                {"Put": {"TableName": self.table_name, "Item": self._marshal(result), "ConditionExpression": "attribute_not_exists(PK)"}},
            ]
            if decision["createCustomer"]:
                item = {
                    "PK": pk, "SK": f"CUSTOMER#{customer_id}", "entity": "customer", "id": customer_id, "business_id": business_id,
                    "display_name": customer["displayName"], "email": customer["email"], "created_at": now,
                    "external_id": customer["externalId"], "origin": "import", "import_id": import_id,
                }
                writes.append({"Put": {"TableName": self.table_name, "Item": self._marshal(item), "ConditionExpression": "attribute_not_exists(PK)"}})
            if decision["createCharge"]:
                counter = self._get(business_id, "FOLIO_COUNTER") or {"value": 0}
                sequence = int(counter["value"]) + 1
                item = {
                    "PK": pk, "SK": f"CHARGE#{charge_id}", "entity": "charge", "id": charge_id, "business_id": business_id,
                    "customer_id": customer_id, "folio": f"{business['folio_prefix']}-{sequence:06d}",
                    "amount_minor": charge["amountMinor"], "allocated_minor": 0, "outstanding_minor": charge["amountMinor"],
                    "currency": charge["currency"], "description": charge["description"], "due_date": charge["dueDate"],
                    "cancelled_at": None, "created_at": now, "creation_key": None,
                    "external_id": charge["externalId"], "origin": "import", "import_id": import_id,
                }
                writes.extend([
                    {"Update": {"TableName": self.table_name, "Key": {"PK": pk, "SK": "FOLIO_COUNTER"},
                                "UpdateExpression": "SET #value = :sequence", "ConditionExpression": "attribute_not_exists(#value) OR #value = :previous",
                                "ExpressionAttributeNames": {"#value": "value"}, "ExpressionAttributeValues": {":sequence": sequence, ":previous": sequence - 1}}},
                    {"Put": {"TableName": self.table_name, "Item": self._marshal(item), "ConditionExpression": "attribute_not_exists(PK)"}},
                ])
            try:
                self.client.transact_write_items(TransactItems=writes)
                return
            except ClientError as error:
                if not self._conditional(error):
                    raise
                # Do not guess which condition failed; ask the state.
                current = self._import_metadata(business_id, import_id)
                if current is None or current["status"] != "applying" or current.get("apply_lease_token") != token:
                    raise _ApplyLeaseLost() from error
                if self._get(business_id, result_sk):
                    return  # this row was already decided (by a retry or another worker)
                if attempt >= self._APPLY_ROW_MAX_ATTEMPTS:
                    raise RuntimeError("import row conflicted repeatedly; retry with the same idempotency key") from error
                _backoff_sleep(attempt)

    def _release_apply_lease(self, business_id, import_id, token, error_code=None):
        """Give the lease back (so the next call resumes at once instead of
        waiting for expiry), recording a recoverable error code when the
        slice stopped on one. Best effort: if this fails the lease simply
        expires by itself."""
        metadata = self._import_metadata(business_id, import_id)
        if metadata is None:
            return
        update = "REMOVE apply_lease_token, apply_lease_expires_at"
        values = {":token": token}
        if error_code:
            update = "SET apply_last_error = :error " + update
            values[":error"] = error_code
        try:
            self.table.update_item(
                Key={"PK": metadata["PK"], "SK": metadata["SK"]}, UpdateExpression=update,
                ConditionExpression="apply_lease_token = :token", ExpressionAttributeValues=values,
            )
        except ClientError:
            pass

    def _finalize_apply(self, business_id, import_id, token, subject_id, idempotency_key, clock):
        """Close the job: applied when every row was created/reused, review
        when at least one row is in conflict. One transaction (metadata
        Update fenced by the lease + audit) so the state and its evidence
        appear together."""
        metadata = self._import_metadata(business_id, import_id)
        now = clock()
        progress = self._apply_progress(business_id, import_id, metadata, now)
        reviewing = progress["conflicts"] > 0
        action = "import.apply_review" if reviewing else "import.applied"
        audit = {
            "PK": _business_key(business_id), "SK": f"AUDIT#{action}#{import_id}", "entity": "audit", "id": _uuid(),
            "business_id": business_id, "action": action, "aggregate_id": import_id, "operation_key": idempotency_key,
            "actor_id": subject_id, "occurred_at": _iso(now),
            "details": {key: progress[key] for key in ("rowsTotal", "rowsProcessed", "customersCreated", "customersReused",
                                                       "chargesCreated", "chargesReused", "conflicts")},
        }
        try:
            self.client.transact_write_items(TransactItems=[
                {"Update": {
                    "TableName": self.table_name, "Key": {"PK": metadata["PK"], "SK": metadata["SK"]},
                    "UpdateExpression": "SET #s = :final, apply_completed_at = :now REMOVE apply_lease_token, apply_lease_expires_at, apply_last_error",
                    "ConditionExpression": "apply_lease_token = :token AND #s = :applying", "ExpressionAttributeNames": {"#s": "status"},
                    "ExpressionAttributeValues": {":final": "review" if reviewing else "applied", ":now": _iso(now), ":token": token, ":applying": "applying"},
                }},
                {"Put": {"TableName": self.table_name, "Item": self._marshal(audit), "ConditionExpression": "attribute_not_exists(PK)"}},
            ])
        except ClientError as error:
            if not self._conditional(error):
                raise
            # Someone else finalized first (or a purge fenced us); the caller
            # returns whatever the authoritative state now is.

    def apply_import_job(self, business_id, import_id, subject_id, idempotency_key, clock, *,
                         max_rows=imports_module.APPLY_ROWS_PER_INVOCATION,
                         time_budget_s=imports_module.APPLY_TIME_BUDGET_SECONDS,
                         lease_seconds=imports_module.APPLY_LEASE_SECONDS, monotonic=time.monotonic):
        """Run one bounded slice of the job's apply and return the job with
        server-derived progress. Safe to call again with the same
        Idempotency-Key until the status is `applied` or `review`: a repeat
        after completion returns the stored result and writes nothing.

        Refuses (ImportOperationRefused) unless the job is `validated` (or an
        `applying` job being resumed); returns None for an unknown import.
        """
        now = clock()
        metadata = self._import_metadata(business_id, import_id)
        if metadata is None:
            if self._purged_import_view(business_id, import_id) is not None:
                raise ImportOperationRefused("import_not_applicable", "purged")
            return None
        guard_sk = f"IMPORT_APPLY_KEY#{import_id}#" + hashlib.sha256(f"{subject_id}\n{idempotency_key}".encode()).hexdigest()
        status = metadata["status"]
        if status in ("applied", "review"):
            if self._get(business_id, guard_sk):
                return self._apply_view(business_id, import_id, metadata, now)
            raise ImportOperationRefused("import_not_applicable", status)
        if status not in ("validated", "applying"):
            raise ImportOperationRefused("import_not_applicable", status)

        chunks = self._load_apply_rows(business_id, import_id, metadata, now)
        outcome, token = self._acquire_apply_lease(business_id, import_id, subject_id, idempotency_key, guard_sk, clock, lease_seconds)
        if outcome != "leased":
            return self._apply_view(business_id, import_id, None, clock())

        metadata = self._import_metadata(business_id, import_id)
        business = self._get(business_id, "BUSINESS")
        if business is None:
            self._release_apply_lease(business_id, import_id, token, "business_not_found")
            raise ValueError("business not found")
        total = sum(len(chunk) for chunk in chunks)
        decided = {(int(item["chunk_index"]), int(item["position"])) for item in self._items(business_id, f"IMPORT_APPLYROW#{import_id}#")}
        deadline = monotonic() + time_budget_s
        handled, error_code, lost = 0, None, False
        try:
            for chunk_index, chunk in enumerate(chunks):
                for position, row in enumerate(chunk):
                    if (chunk_index, position) in decided:
                        continue
                    if handled >= max_rows or monotonic() >= deadline:
                        raise _SliceFull()
                    self._apply_import_row(business_id, import_id, business, metadata, token, clock, chunk_index, position, row)
                    handled += 1
        except _SliceFull:
            pass
        except _ApplyLeaseLost:
            lost = True
        except ClientError:
            error_code = "dynamodb_error"
        except RuntimeError:
            error_code = "row_conflict_retries_exhausted"

        if not lost:
            remaining = total - len(self._items(business_id, f"IMPORT_APPLYROW#{import_id}#"))
            if error_code is None and remaining == 0:
                self._finalize_apply(business_id, import_id, token, subject_id, idempotency_key, clock)
            else:
                self._release_apply_lease(business_id, import_id, token, error_code)
        return self._apply_view(business_id, import_id, None, clock())

    _BY_ID_PREFIX = {"payment": "PAYMENT", "outbox": "OUTBOX", "refund": "REFUND", "provider_event": "EVENT"}

    def _by_id(self, entity, item_id):
        # Every caller here resolves exactly one item across the whole
        # tenant's table by its own id, not by business/partition -- a
        # table-wide Scan(Limit=100) examines (not finds) at most 100 items
        # total across every business before giving up, so it silently
        # returns None for real items once the table grows past that. Each
        # entity carries a GSI1PK keyed by its own id precisely so this can
        # be a targeted LookupIndex query instead.
        return self._lookup(f"{self._BY_ID_PREFIX[entity]}#{item_id}")

    def _claim_work(self, kind, now, lease_expires_at, limit, entity, statuses, counter):
        from boto3.dynamodb.conditions import Key
        claimed, token = [], _uuid()
        status_name = "processing_status" if entity == "provider_event" else "status"
        # `lease_token`/`lease_expires_at` are the *only* claim/exclusion
        # mechanism. A claim must never overwrite status/processing_status:
        # that field is the item's real logical state (creating/unknown/ready/
        # expiring, or requested/pending/accepted/etc for the other kinds),
        # and finish_*/mark_*/fail_* methods elsewhere in this file read it
        # back with a fresh, strongly consistent GET. Earlier this method set
        # it to a transient "processing" value during the lease, which broke
        # every one of those re-reads (e.g. mark_attempt_ready rejects a
        # status it doesn't recognize) and, once the lease was released
        # without anyone restoring the original value, left the item
        # permanently invisible to future claims -- not just for
        # reconciliation, but for any entity this method serves.
        status_placeholders = {f":claimable{i}": value for i, value in enumerate(statuses)}
        query = {
            "IndexName": "WorkIndex",
            "KeyConditionExpression": Key("GSI3PK").eq(f"WORK#{kind}"),
            "Limit": min(max(int(limit) * 4, 1), 100),
        }
        # A single page can be entirely items that finished long ago but
        # haven't been cleaned out of this index yet (or, before this fix,
        # never could be). Keep paging with ExclusiveStartKey instead of
        # giving up after one page, so real pending work sitting behind a
        # wall of old ones is still reachable. The page-count cap is a
        # runaway guard, not an expected ceiling now that finish_*/mark_*/
        # fail_* methods remove GSI3PK/GSI3SK from an item once it leaves a
        # claimable state.
        for _ in range(20):
            if len(claimed) >= limit:
                break
            response = self.table.query(**query)
            for item in response.get("Items", []):
                if len(claimed) >= limit or item.get("entity") != entity or item.get(status_name) not in statuses or item.get("available_at", item.get("reconcile_after", "")) > _iso(now):
                    continue
                try:
                    self.table.update_item(
                        Key={"PK": item["PK"], "SK": item["SK"]},
                        UpdateExpression="SET lease_token=:token, lease_expires_at=:expires, #counter=:count",
                        ConditionExpression="(attribute_not_exists(lease_token) OR lease_expires_at < :now) AND #state IN ({})".format(",".join(status_placeholders)),
                        ExpressionAttributeNames={"#state": status_name, "#counter": counter},
                        ExpressionAttributeValues={**status_placeholders, ":token": token, ":expires": _iso(lease_expires_at), ":now": _iso(now), ":count": int(item.get(counter, 0)) + 1},
                    )
                except ClientError as error:
                    if self._conditional(error):
                        continue
                    raise
                value = dict(item)
                value.update({"lease_token": token, "lease_expires_at": _iso(lease_expires_at), counter: int(item.get(counter, 0)) + 1})
                claimed.append(value)
            last_key = response.get("LastEvaluatedKey")
            if not last_key:
                break
            query["ExclusiveStartKey"] = last_key
        return claimed

    def claim_provider_events(self, now, lease_expires_at, limit=25):
        return self._claim_work("provider_event", now, lease_expires_at, limit, "provider_event", ("accepted", "failed", "processing"), "processing_attempt_count")

    def fail_provider_event(self, event_id, lease_token, error, available_at, terminal=False):
        event = self._by_id("provider_event", event_id)
        if not event:
            return
        # terminal=True means "review" -- paused, not claimable -- so it
        # drops the WorkIndex entry too (restored by resolve_review's
        # "retry"); a plain "failed" stays claimable and keeps it.
        removes = ", GSI3PK, GSI3SK" if terminal else ""
        self.table.update_item(Key={"PK": event["PK"], "SK": event["SK"]}, UpdateExpression=f"SET processing_status=:status, processing_error=:error, available_at=:available REMOVE lease_token, lease_expires_at{removes}", ConditionExpression="lease_token=:token", ExpressionAttributeValues={":status": "review" if terminal else "failed", ":error": str(error)[:500], ":available": _iso(available_at), ":token": lease_token})

    def claim_reconciliation_attempts(self, now, lease_expires_at, limit=25):
        values = self._claim_work("reconciliation", now, lease_expires_at, limit, "attempt", ("creating", "unknown", "ready", "expiring"), "recovery_attempt_count")
        for value in values:
            value["recovery_lease_token"] = value.pop("lease_token")
            value["recovery_lease_expires_at"] = value.pop("lease_expires_at")
        return values

    def finish_reconciliation(self, attempt_id, lease_token, next_at, error=None):
        attempt = self._attempt_by_id(attempt_id)
        if not attempt:
            return
        self.table.update_item(Key={"PK": attempt["PK"], "SK": attempt["SK"]}, UpdateExpression="SET available_at=:next, recovery_error=:error, updated_at=:updated REMOVE lease_token, lease_expires_at", ConditionExpression="lease_token=:token", ExpressionAttributeValues={":next": _iso(next_at), ":error": str(error)[:500] if error else None, ":updated": _iso(datetime.now(timezone.utc)), ":token": lease_token})

    def claim_outbox(self, now, lease_expires_at, limit=25):
        return self._claim_work("outbox", now, lease_expires_at, limit, "outbox", ("pending", "failed", "processing"), "attempt_count")

    def finish_outbox(self, message_id, lease_token, now):
        message = self._by_id("outbox", message_id)
        if message:
            # Terminal: "sent" is never claimable again, so this also drops
            # the message out of WorkIndex instead of leaving a dead entry.
            self.table.update_item(Key={"PK": message["PK"], "SK": message["SK"]}, UpdateExpression="SET #status=:sent, sent_at=:now REMOVE lease_token, lease_expires_at, GSI3PK, GSI3SK", ConditionExpression="lease_token=:token", ExpressionAttributeNames={"#status": "status"}, ExpressionAttributeValues={":sent": "sent", ":now": _iso(now), ":token": lease_token})

    def fail_outbox(self, message_id, lease_token, error, available_at):
        message = self._by_id("outbox", message_id)
        if message:
            self.table.update_item(Key={"PK": message["PK"], "SK": message["SK"]}, UpdateExpression="SET #status=:failed, last_error=:error, available_at=:available REMOVE lease_token, lease_expires_at", ConditionExpression="lease_token=:token", ExpressionAttributeNames={"#status": "status"}, ExpressionAttributeValues={":failed": "failed", ":error": str(error)[:500], ":available": _iso(available_at), ":token": lease_token})

    def notification_details(self, business_id, charge_id):
        charge = self._get(business_id, f"CHARGE#{charge_id}")
        if not charge:
            return None
        customer, business = self._get(business_id, f"CUSTOMER#{charge['customer_id']}"), self._get(business_id, "BUSINESS")
        return {"business_name": (business or {}).get("display_name"), "folio": charge["folio"], "description": charge["description"], "currency": charge["currency"], "outstanding_minor": int(charge["outstanding_minor"]), "customer_name": (customer or {}).get("display_name"), "email": (customer or {}).get("email")}

    def operational_health(self, now):
        values = self.table.scan().get("Items", [])
        return {"providerEvents": sum(item.get("entity") == "provider_event" and item.get("processing_status") in ("accepted", "failed", "review") for item in values), "outbox": sum(item.get("entity") == "outbox" and item.get("status") in ("pending", "failed") for item in values), "refunds": sum(item.get("entity") == "refund" and item.get("status") == "review" for item in values)}
