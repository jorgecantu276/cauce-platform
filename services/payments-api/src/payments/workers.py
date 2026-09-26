"""Leased workers for provider events, reconciliation, and transactional outbox."""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging

from payments.models import CheckoutRequest, PaymentReviewIdentityConflict
from payments.service import ProviderOutcomeUnknown


LOGGER = logging.getLogger(__name__)


def _log_worker_failure(worker, error, **context):
    """Emit searchable failure metadata without serializing payloads or errors.

    Exception messages can contain provider responses or customer data, so the
    log intentionally records only the exception class and stable identifiers.
    The durable, operator-facing error code remains on the DynamoDB work item.
    """
    record = {
        "event": "payment_worker_failure",
        "worker": worker,
        "errorType": error.__class__.__name__,
        **{key: value for key, value in context.items() if value is not None},
    }
    LOGGER.error(json.dumps(record, separators=(",", ":"), sort_keys=True))


def _now(value=None):
    return value or datetime.now(timezone.utc)


def _retry_at(now, attempt_count, maximum_seconds=3600):
    seconds = min(5 * (2 ** min(max(int(attempt_count) - 1, 0), 9)), maximum_seconds)
    return now + timedelta(seconds=seconds)


def run_provider_event_worker(runtime, *, now=None, limit=25):
    now = _now(now)
    repository = runtime.repository()
    events = repository.claim_provider_events(now, now + timedelta(seconds=45), limit)
    result = {"claimed":len(events),"processed":0,"failed":0,"review":0}
    for event in events:
        try:
            if event["event_type"] not in ("payment", "reconciliation.payment", "payment_review.retry"):
                repository.fail_provider_event(
                    event["id"],event["lease_token"],"unsupported_event_type",now,terminal=True
                )
                result["review"] += 1
                continue
            if event["event_type"] == "payment_review.retry" and (
                event.get("signature_valid") is not False
                or (event.get("raw_payload") or {}).get("source") != "owner_review_retry"
            ):
                repository.fail_provider_event(
                    event["id"], event["lease_token"], "invalid_internal_retry_event", now, terminal=True
                )
                result["review"] += 1
                continue
            context = runtime.payment_context(str(event["merchant_connection_id"]))
            if event["event_type"] == "payment_review.retry":
                assessment = context.flow.reconcile_review_payment(
                    str(event["provider_event_key"]), str(event["provider_resource_id"]),
                    str(event["raw_payload"]["reviewId"]), str(event["business_id"]),
                    str(event["merchant_connection_id"]),
                )
            else:
                assessment = context.flow.reconcile_payment(
                    str(event["provider_event_key"]),
                    str(event["provider_resource_id"]),
                    expected_connection_id=str(event["merchant_connection_id"]),
                )
            if assessment.review_reason:
                result["review"] += 1
            else:
                result["processed"] += 1
        except PaymentReviewIdentityConflict as exc:
            _log_worker_failure(
                "provider_event", exc, eventId=event.get("id"),
                businessId=event.get("business_id"),
                connectionId=event.get("merchant_connection_id"),
                attemptCount=event.get("processing_attempt_count"),
            )
            repository.fail_provider_event(
                event["id"], event["lease_token"], "payment_review_identity_conflict",
                now, terminal=True,
            )
            result["review"] += 1
        except Exception as exc:
            _log_worker_failure(
                "provider_event",exc,eventId=event.get("id"),
                businessId=event.get("business_id"),
                connectionId=event.get("merchant_connection_id"),
                attemptCount=event.get("processing_attempt_count"),
            )
            repository.fail_provider_event(
                event["id"],event["lease_token"],"authoritative_reconciliation_failed",
                _retry_at(now,event["processing_attempt_count"]),
            )
            result["failed"] += 1
    return result


def _recover_preference(context, attempt_id):
    payment_context = context.repository.payment_context(str(attempt_id))
    if payment_context is None:
        return False
    attempt, connection, charge = payment_context
    recovered = context.flow.provider.recover_checkout(attempt.id)
    if recovered is None:
        return False
    request = CheckoutRequest(
        external_reference=attempt.id,
        operation_key=attempt.operation_key,
        description=charge.description,
        amount_minor=attempt.expected_amount_minor,
        currency=attempt.currency,
        provider_account_id=connection.provider_account_id,
        environment=connection.environment,
    )
    context.flow._validate_checkout(request, recovered)
    context.repository.mark_attempt_ready(attempt.business_id, attempt.id, recovered, context.flow.clock())
    return True


def run_reconciliation_worker(runtime, *, now=None, limit=25):
    now = _now(now)
    repository = runtime.repository()
    attempts = repository.claim_reconciliation_attempts(
        now,now + timedelta(seconds=90),limit
    )
    result = {"claimed":len(attempts),"paymentsQueued":0,"recovered":0,"failed":0}
    for attempt in attempts:
        lease_token = attempt["recovery_lease_token"]
        try:
            context = runtime.payment_context(str(attempt["merchant_connection_id"]))
            if attempt["status"] == "expiring":
                context.flow.provider.expire_checkout(
                    str(attempt["provider_preference_id"]),now
                )
            if attempt["status"] in ("creating", "unknown"):
                if _recover_preference(context,str(attempt["id"])):
                    result["recovered"] += 1
            summaries = context.flow.provider.search_payments(str(attempt["id"]))
            for summary in summaries:
                evidence = "\n".join((
                    str(attempt["id"]),summary["id"],summary.get("updated_at") or "unknown"
                ))
                event_key = "reconciliation:" + hashlib.sha256(evidence.encode()).hexdigest()
                captured = context.repository.capture_provider_event(
                    str(attempt["merchant_connection_id"]),event_key,summary["id"],
                    "reconciliation.payment",
                    {"source":"scheduled_reconciliation","data":{"id":summary["id"]}},
                    False,now,
                )
                if captured["status"] in ("accepted", "failed"):
                    result["paymentsQueued"] += 1
            if attempt["status"] == "expiring":
                # Terminal: mark_attempt_expired already released this lease
                # (and the ACTIVE_ATTEMPT guard) as part of retiring the
                # attempt, so there is no further reconciliation to schedule
                # and no lease left for finish_reconciliation to release.
                context.repository.mark_attempt_expired(str(attempt["id"]),lease_token,now)
            else:
                next_at = now + timedelta(hours=1 if summaries else 5 / 60)
                repository.finish_reconciliation(attempt["id"],lease_token,next_at)
        except Exception as exc:
            _log_worker_failure(
                "reconciliation",exc,attemptId=attempt.get("id"),
                businessId=attempt.get("business_id"),
                connectionId=attempt.get("merchant_connection_id"),
                attemptCount=attempt.get("recovery_attempt_count"),
            )
            repository.finish_reconciliation(
                attempt["id"],lease_token,
                _retry_at(now,attempt["recovery_attempt_count"]),
                "scheduled_reconciliation_failed",
            )
            result["failed"] += 1
    return result


def run_outbox_worker(runtime, sender, *, now=None, limit=25):
    now = _now(now)
    repository = runtime.repository()
    messages = repository.claim_outbox(now,now + timedelta(seconds=45),limit)
    result = {"claimed":len(messages),"sent":0,"failed":0}
    for message in messages:
        try:
            charge_id = (message["payload"] or {}).get("chargeId")
            details = repository.notification_details(message["business_id"],charge_id)
            sender.send(message,details)
            repository.finish_outbox(message["id"],message["lease_token"],now)
            result["sent"] += 1
        except Exception as exc:
            _log_worker_failure(
                "outbox",exc,messageId=message.get("id"),
                businessId=message.get("business_id"),
                attemptCount=message.get("attempt_count"),
            )
            repository.fail_outbox(
                message["id"],message["lease_token"],"notification_delivery_failed",
                _retry_at(now,message["attempt_count"]),
            )
            result["failed"] += 1
    return result


def run_refund_worker(runtime, *, now=None, limit=10):
    now = _now(now)
    repository = runtime.repository()
    operations = repository.claim_refund_operations(
        now,now + timedelta(seconds=90),limit
    )
    result = {"claimed":len(operations),"completed":0,"retrying":0,"review":0}
    for operation in operations:
        try:
            connection_id = str(operation["merchant_connection_id"])
            context = runtime.payment_context(connection_id)
            provider_payment_id = repository.refund_payment_reference(
                operation["business_id"],operation["payment_id"]
            )
            if not provider_payment_id:
                raise RuntimeError("payment reference is unavailable")
            provider_refund_id = operation["provider_refund_id"]
            if not provider_refund_id:
                response = context.flow.provider.request_refund(
                    provider_payment_id,str(operation["operation_key"]),
                    None if operation["full_refund"] else operation["amount_minor"],
                )
                provider_refund_id = response["id"] or None
            event_key = "refund-reconciliation:%s:%s" % (
                operation["operation_key"],operation["attempt_count"]
            )
            context.repository.capture_provider_event(
                connection_id,event_key,provider_payment_id,"reconciliation.payment",
                {"source":"refund_operation","refundId":str(operation["id"])},False,now,
            )
            context.flow.reconcile_payment(
                event_key,provider_payment_id,expected_connection_id=connection_id
            )
            completed = repository.refund_reconciled(
                operation["id"],operation["lease_token"],provider_refund_id
            )
            repository.finish_refund_operation(
                operation["id"],operation["lease_token"],
                "completed" if completed else "accepted",now,
                provider_refund_id=provider_refund_id,
                available_at=None if completed else now + timedelta(minutes=5),
            )
            result["completed" if completed else "retrying"] += 1
        except ProviderOutcomeUnknown as exc:
            _log_worker_failure(
                "refund",exc,refundId=operation.get("id"),
                businessId=operation.get("business_id"),
                paymentId=operation.get("payment_id"),
                connectionId=operation.get("merchant_connection_id"),
                attemptCount=operation.get("attempt_count"),outcome="retry",
            )
            repository.finish_refund_operation(
                operation["id"],operation["lease_token"],"unknown",now,
                error="provider_outcome_unknown",
                available_at=_retry_at(now,operation["attempt_count"]),
            )
            result["retrying"] += 1
        except RuntimeError as exc:
            _log_worker_failure(
                "refund",exc,refundId=operation.get("id"),
                businessId=operation.get("business_id"),
                paymentId=operation.get("payment_id"),
                connectionId=operation.get("merchant_connection_id"),
                attemptCount=operation.get("attempt_count"),outcome="review",
            )
            repository.finish_refund_operation(
                operation["id"],operation["lease_token"],"review",now,
                error=str(exc),
            )
            result["review"] += 1
        except Exception as exc:
            _log_worker_failure(
                "refund",exc,refundId=operation.get("id"),
                businessId=operation.get("business_id"),
                paymentId=operation.get("payment_id"),
                connectionId=operation.get("merchant_connection_id"),
                attemptCount=operation.get("attempt_count"),outcome="review",
            )
            repository.finish_refund_operation(
                operation["id"],operation["lease_token"],"review",now,
                error="refund_submission_failed:" + exc.__class__.__name__,
            )
            result["review"] += 1
    return result


def run_operational_health(runtime, *, now=None):
    health = runtime.repository().operational_health(_now(now))
    if any(health.values()):
        raise RuntimeError("payment processing health check failed")
    return {"healthy":True,**health}
