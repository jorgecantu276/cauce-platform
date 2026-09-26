from payments.runtime import Runtime
from payments.workers import run_operational_health


def handler(event, context):
    return run_operational_health(Runtime())
