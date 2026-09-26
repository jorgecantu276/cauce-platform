from payments.runtime import Runtime
from payments.transport import handle_webhook


def handler(event, context):
    return handle_webhook(event, Runtime())
