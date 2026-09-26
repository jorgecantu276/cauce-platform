import os

from payments.notifications import SesNotificationSender
from payments.runtime import Runtime
from payments.workers import run_outbox_worker


def handler(event, context):
    sender = SesNotificationSender(os.environ.get("NOTIFICATION_FROM_EMAIL"))
    return run_outbox_worker(Runtime(),sender)
