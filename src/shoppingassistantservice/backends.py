"""Agent tools: thin wrappers over the existing Online Boutique gRPC services.

Every tool returns a plain dict. On backend failure it returns
{"error": ..., "grpc_status": ...} (never raises, never fabricates success) so
the model can report the real outcome. The agent layer turns those into span
status / metrics.
"""
import datetime

import grpc

import config
import demo_pb2 as pb
import demo_pb2_grpc as pbg

_channels = {}


def _stub(addr, stub_cls):
    if addr not in _channels:
        _channels[addr] = grpc.insecure_channel(addr)
    return stub_cls(_channels[addr])


def _catalog():
    return _stub(config.CATALOG_ADDR, pbg.ProductCatalogServiceStub)


def _cart():
    return _stub(config.CART_ADDR, pbg.CartServiceStub)


def _currency():
    return _stub(config.CURRENCY_ADDR, pbg.CurrencyServiceStub)


def _checkout():
    return _stub(config.CHECKOUT_ADDR, pbg.CheckoutServiceStub)


def _money_usd(m):
    return round(m.units + m.nanos / 1e9, 2)


def _fmt_money(m):
    return {"currency_code": m.currency_code, "amount": round(m.units + m.nanos / 1e9, 2)}


def _product(p):
    return {"id": p.id, "name": p.name, "description": p.description,
            "price_usd": _money_usd(p.price_usd), "categories": list(p.categories)}


def _err(e):
    code = e.code().name if hasattr(e, "code") and e.code() else "UNKNOWN"
    details = e.details() or ""
    out = {"error": f"{code}: {details}", "grpc_status": code}
    if details.startswith("reason:"):  # checkoutservice's stable reason code
        out["reason"] = details[len("reason:"):]
    return out


_price_cache = {}


def _price_usd(product_id):
    if product_id not in _price_cache:
        p = _catalog().GetProduct(pb.GetProductRequest(id=product_id), timeout=config.GRPC_TIMEOUT_S)
        _price_cache[product_id] = (p.name, _money_usd(p.price_usd))
    return _price_cache[product_id]


_supported = {"codes": None}


def supported_currencies():
    """Cached list of codes the currency service accepts (observability only -
    never used to gate or repair tool arguments)."""
    if _supported["codes"] is None:
        try:
            r = _currency().GetSupportedCurrencies(pb.Empty(), timeout=config.GRPC_TIMEOUT_S)
            _supported["codes"] = set(r.currency_codes)
        except grpc.RpcError:
            return set()
    return _supported["codes"]


# --- tools ---------------------------------------------------------------------
def search_products(user_id, query):
    try:
        found, seen = [], set()
        terms = [query] + [w for w in query.split() if w.lower() != query.lower()]
        for term in terms:
            r = _catalog().SearchProducts(pb.SearchProductsRequest(query=term), timeout=config.GRPC_TIMEOUT_S)
            for p in r.results:
                if p.id not in seen:
                    seen.add(p.id)
                    found.append(_product(p))
            if found:
                break
        return {"results": found, "count": len(found)}
    except grpc.RpcError as e:
        return _err(e)


def get_product(user_id, product_id):
    try:
        return _product(_catalog().GetProduct(pb.GetProductRequest(id=product_id), timeout=config.GRPC_TIMEOUT_S))
    except grpc.RpcError as e:
        return _err(e)


def convert_price(user_id, amount_usd, currency_code):
    units = int(amount_usd)
    nanos = int(round((amount_usd - units) * 1e9))
    try:
        out = _currency().Convert(
            pb.CurrencyConversionRequest(
                **{"from": pb.Money(currency_code="USD", units=units, nanos=nanos)}, to_code=currency_code),
            timeout=config.GRPC_TIMEOUT_S)
        return {"converted": _fmt_money(out)}
    except grpc.RpcError as e:
        return _err(e)


def add_to_cart(user_id, product_id, quantity):
    try:
        _cart().AddItem(pb.AddItemRequest(
            user_id=user_id, item=pb.CartItem(product_id=product_id, quantity=int(quantity))),
            timeout=config.GRPC_TIMEOUT_S)
        return {"added": {"product_id": product_id, "quantity": int(quantity)}}
    except grpc.RpcError as e:
        return _err(e)


def cart_contents(user_id):
    """(items, value_usd) - used by get_cart and for checkout value telemetry."""
    cart = _cart().GetCart(pb.GetCartRequest(user_id=user_id), timeout=config.GRPC_TIMEOUT_S)
    items, total = [], 0.0
    for it in cart.items:
        name, price = _price_usd(it.product_id)
        total += price * it.quantity
        items.append({"product_id": it.product_id, "name": name, "quantity": it.quantity,
                      "unit_price_usd": price})
    return items, round(total, 2)


def get_cart(user_id):
    try:
        items, total = cart_contents(user_id)
        return {"items": items, "total_usd": total}
    except grpc.RpcError as e:
        return _err(e)


def _zip_int(z):
    digits = "".join(c for c in str(z) if c.isdigit())
    return int(digits[:9] or 0)


def place_order(user_id, currency_code, email, address, card_number, idempotency_key="", account_token="", payment_method_id=""):
    """Charge the cart. `card_number` is a synthetic TEST card resolved from a payment
    method id; it is only handed to checkoutservice and is never logged or traced.
    `idempotency_key` makes a retried call safe (checkout replays instead of charging twice);
    `account_token` lets checkout attribute the order to the signed-in customer."""
    year = datetime.date.today().year + 2
    md = [("x-account-session", account_token)] if account_token else None
    try:
        resp = _checkout().PlaceOrder(pb.PlaceOrderRequest(
            user_id=user_id,
            user_currency=currency_code,
            address=pb.Address(street_address=address["street_address"], city=address["city"],
                               state=address["state"], country=address["country"],
                               zip_code=_zip_int(address["zip_code"]),
                               postal_code=str(address.get("zip_code") or "")),
            email=email,
            # A saved method is charged by token inside checkout: no card data is sent at all.
            credit_card=None if payment_method_id else pb.CreditCardInfo(
                credit_card_number=card_number, credit_card_cvv=672,
                credit_card_expiration_year=year, credit_card_expiration_month=1),
            payment_method_id=payment_method_id or "",
            idempotency_key=idempotency_key, channel="assistant"),
            timeout=config.GRPC_TIMEOUT_S, metadata=md)
        o = resp.order
        return {
            "status": resp.status or "completed",
            "record_status": resp.record_status or "recorded",
            "replayed": resp.replayed,
            "order_id": o.order_id,
            "shipping_tracking_id": o.shipping_tracking_id,
            "shipping_cost": _fmt_money(o.shipping_cost),
            "items": [{"product_id": i.item.product_id, "quantity": i.item.quantity,
                       "cost": _fmt_money(i.cost)} for i in o.items],
        }
    except grpc.RpcError as e:
        return _err(e)


TOOLS = {
    "search_products": search_products,
    "get_product": get_product,
    "convert_price": convert_price,
    "add_to_cart": add_to_cart,
    "get_cart": get_cart,
}
