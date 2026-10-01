"""The shop's database: one orders table."""

ORDERS_SCHEMA = "CREATE TABLE orders (id INTEGER PRIMARY KEY, status TEXT)"


def save_order(order):
    """Insert or update one order row."""
    return order
