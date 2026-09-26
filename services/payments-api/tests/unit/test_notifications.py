import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "../../src"))

from payments.notifications import SesNotificationSender


def test_non_customer_events_are_completed_without_an_endless_delivery_retry():
    sender = SesNotificationSender("sender@example.test", client=object())
    assert sender.send({"topic":"payment_observed"}, None) is None
    assert sender.send({"topic":"payment_review"}, None) is None
