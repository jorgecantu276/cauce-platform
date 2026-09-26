"""Notification delivery kept outside the financial transaction."""


class SesNotificationSender:
    def __init__(self, from_email, client=None):
        if not from_email:
            raise RuntimeError("notification sender is not configured")
        if client is None:
            import boto3
            client = boto3.client("sesv2")
        self.from_email = from_email
        self.client = client

    def send(self, message, details):
        if message["topic"] == "payment_observed":
            # This is financial telemetry, not a customer-facing payment
            # confirmation. The durable payment record and review queue are
            # the operator surfaces, so the outbox item is intentionally
            # completed without sending a misleading message.
            return
        if message["topic"] == "payment_review":
            # Review work is visible to owners through the authenticated work
            # queue. Do not retry an email indefinitely when no operator
            # delivery channel has been configured for this first pilot.
            return
        if message["topic"] != "payment_approved":
            raise RuntimeError("unsupported outbox topic")
        if not details or not details.get("email"):
            # Payment status remains available through the secure link. Email
            # is optional customer data and cannot block financial processing.
            return
        amount = details["outstanding_minor"]
        subject = f"Pago recibido · {details['folio']}"
        body = (
            f"Hola {details['customer_name']},\n\n"
            f"{details['business_name']} recibió tu pago para "
            f"{details['description']}.\n"
            f"Folio: {details['folio']}\n"
            f"Saldo pendiente: {details['currency']} {amount / 100:.2f}\n"
        )
        self.client.send_email(
            FromEmailAddress=self.from_email,
            Destination={"ToAddresses":[details["email"]]},
            Content={"Simple":{"Subject":{"Data":subject,"Charset":"UTF-8"},
                               "Body":{"Text":{"Data":body,"Charset":"UTF-8"}}}},
        )
