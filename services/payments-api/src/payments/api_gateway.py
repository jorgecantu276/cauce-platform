def request_base_url(event):
    """Return the API Gateway origin from trusted Lambda event metadata.

    This is deliberately not derived from a browser-provided Host header. It
    also avoids making a SAM function depend on the API resource that invokes
    it, which would create a CloudFormation cycle on the first deployment.
    """
    context = event.get("requestContext") or {}
    domain, stage = context.get("domainName"), context.get("stage")
    if not domain:
        return None
    return "https://" + domain + ("" if not stage or stage == "$default" else "/" + stage)
