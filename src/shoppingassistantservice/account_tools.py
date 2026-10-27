"""Read-only order-history tools for signed-in customers.

Identity comes ONLY from the account-session token the storefront forwarded (never from model arguments);
accountservice enforces ownership, so another customer's order id is simply "not found". Results contain
no street address, email or phone: only what is needed to talk about an order.
"""
import re

import accounts

_ORDER_ID = re.compile(r"^[0-9a-fA-F-]{36}$")


def _login_required():
    return {"status": "login_required",
            "hint": "Order history is only available to signed-in customers. Ask them to log in on the storefront."}


def _client_and_token(session):
    tok = getattr(session, "account_token", "") or ""
    if not tok:
        return None, None
    return accounts.grpc_client(), tok


def _unavailable():
    return {"error": "UNAVAILABLE: order history is temporarily unavailable", "grpc_status": "UNAVAILABLE"}


def list_orders(session, limit=5):
    import account_client
    client, tok = _client_and_token(session)
    if client is None:
        return _login_required()
    try:
        orders, queue_depth = client.orders(tok, limit)
    except account_client.AccountUnavailable:
        return _unavailable()
    out = {"orders": orders, "count": len(orders)}
    if queue_depth:
        out["note"] = "Some very recent orders may take a moment to appear."
    return out


def get_order(session, order_id):
    import account_client
    client, tok = _client_and_token(session)
    if client is None:
        return _login_required()
    if not _ORDER_ID.match(str(order_id or "")):
        return {"status": "not_found", "hint": "Use an order_id returned by list_orders."}
    try:
        return client.order(tok, order_id)
    except account_client.AccountUnavailable:
        return _unavailable()
    except Exception as e:  # noqa: BLE001 - NOT_FOUND from accountservice (also for other customers' orders)
        import grpc
        if isinstance(e, grpc.RpcError) and e.code() == grpc.StatusCode.NOT_FOUND:
            return {"status": "not_found", "hint": "No such order for this customer."}
        raise


TOOLS = {"list_orders": list_orders, "get_order": get_order}
