from payments.api_gateway import request_base_url
from payments.platform_transport import handle_platform
from payments.runtime import Runtime
from payments.staff_transport import handle_staff


def handler(event, context):
    runtime = Runtime(public_api_base_url=request_base_url(event))
    route_key = (event.get("requestContext") or {}).get("routeKey", "")
    # Platform routes are a distinct trust boundary (a verified JWT group,
    # not tenant membership) with their own transport module -- see
    # platform_transport.py's docstring for why this is a hard dispatch
    # split, not a branch inside handle_staff.
    if "/platform/" in route_key:
        return handle_platform(event, runtime)
    return handle_staff(event, runtime)
