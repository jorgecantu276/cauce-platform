from payments.runtime import Runtime
from payments.workers import run_provider_event_worker


def handler(event, context):
    return run_provider_event_worker(Runtime())
