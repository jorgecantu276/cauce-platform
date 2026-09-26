from payments.api_gateway import request_base_url
from payments.runtime import Runtime
from payments.transport import handle_public


def handler(event, context):
    return handle_public(event, Runtime(public_api_base_url=request_base_url(event)))
