"""The shop's storefront: renders pages and forwards requests to the API."""


def render_page(name):
    """Return the HTML of a storefront page."""
    return "<h1>%s</h1>" % name


def forward_request(path):
    """Send a browser request on to the API (journal verb: request)."""
    return {"path": path}
