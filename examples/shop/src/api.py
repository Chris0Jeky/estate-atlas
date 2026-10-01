"""The shop's API: takes orders, charges cards, queues fulfilment, stores orders."""


def handle_request(path):
    """Entry point for every storefront request."""
    return {"ok": True, "path": path}


def create_order(items):
    """Store the order, then queue it for fulfilment (journal verbs: query, enqueue)."""
    return {"items": items, "status": "new"}


def charge_card(order, amount):
    """Ask the payments provider to charge (journal verb: charge)."""
    return {"order": order, "amount": amount}
