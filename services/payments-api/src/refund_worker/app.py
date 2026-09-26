from payments.runtime import Runtime
from payments.workers import run_refund_worker


def handler(event, context):
    return run_refund_worker(Runtime())
