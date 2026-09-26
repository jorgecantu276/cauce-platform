from payments.runtime import Runtime
from payments.workers import run_reconciliation_worker


def handler(event, context):
    return run_reconciliation_worker(Runtime())
