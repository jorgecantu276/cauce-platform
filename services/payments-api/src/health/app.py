from common.responses import json_response


def handler(event, context):
    return json_response(200, {"status": "ok"})
