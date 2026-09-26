from urllib.parse import urlparse
import common.db as db


def allowed_origin_header(origin):
    if not origin:
        return {}
    hostname = urlparse(origin).hostname
    if hostname and db.get_business_by_hostname(hostname):
        return {"Access-Control-Allow-Origin": origin}
    return {}
