import json
from common.cors import allowed_origin_header


def json_response(status_code, body, headers=None, origin=None):
    resp_headers = {"Content-Type": "application/json"}
    if origin:
        resp_headers.update(allowed_origin_header(origin))
    if headers:
        resp_headers.update(headers)
    return {
        "statusCode": status_code,
        "headers": resp_headers,
        "body": json.dumps(body),
    }
