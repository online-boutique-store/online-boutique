"""Order-history tools and saved-payment-method checkout in the assistant (ACCOUNT_BACKEND=grpc),
against a fake account client (the real RPCs are covered by accountservice's own suites).

Run from src/shoppingassistantservice:   python tests/test_account_tools.py
"""
import json
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
os.environ["ACCOUNT_BACKEND"] = "grpc"
os.environ["TELEMETRY_REF_KEY"] = "test-ref-key"

import grpc  # noqa: E402

PLACED = []
stub = types.ModuleType("backends")
stub.cart_contents = lambda uid: ([{"product_id": "P1", "name": "Mug", "quantity": 1, "unit_price_usd": 8.0}], 8.0)


def _place_order(user_id, currency, email, address, card, idempotency_key="", account_token="", payment_method_id=""):
    PLACED.append({"card": card, "pm": payment_method_id, "token": account_token, "key": idempotency_key})
    return {"order_id": "ord-1"}


stub.place_order, stub.TOOLS = _place_order, {}
sys.modules["backends"] = stub

import account_client  # noqa: E402
import account_tools  # noqa: E402
import accounts  # noqa: E402
import checkout  # noqa: E402
import config  # noqa: E402
import incident  # noqa: E402
import telemetry as tel  # noqa: E402

OWN = "11111111-2222-4333-8444-555555555555"
OTHER = "99999999-2222-4333-8444-555555555555"
PM_ID = "aaaaaaaa-2222-4333-8444-555555555555"


class NotFound(grpc.RpcError):
    def code(self):
        return grpc.StatusCode.NOT_FOUND


class FakeClient:
    unavailable = False

    def orders(self, token, limit=5):
        if self.unavailable:
            raise account_client.AccountUnavailable("unreachable")
        assert token == "tok-signed-in"
        return [{"order_id": OWN, "placed_at": "2026-10-20", "status": "confirmed", "total": "26.97 USD",
                 "item_count": 2, "items": ["Mug"]}], 1

    def order(self, token, order_id):
        if self.unavailable:
            raise account_client.AccountUnavailable("unreachable")
        if order_id != OWN:
            raise NotFound()
        return {"order_id": OWN, "status": "confirmed", "total": "26.97 USD", "items": [{"name": "Mug", "quantity": 2}],
                "payment": "visa ending 4242"}

    def validate(self, fields, only=None):
        n = {"first_name": "", "last_name": "", "email": str(fields.get("email") or "").lower(), "phone": "",
             "line1": fields.get("address_line1") or "", "line2": fields.get("address_line2") or "",
             "city": fields.get("city") or "", "state": (fields.get("state") or "").upper(),
             "zip": fields.get("zip") or "", "country": "US"}
        return {}, n

    def payment_methods(self, token):
        return [{"id": PM_ID, "brand": "visa", "last4": "4242", "is_default": True}]


class Sess:
    def __init__(self, token=""):
        self.id, self.turns = "s-tools", 0
        self.account_token = token
        self.customer_account = None
        self.checkout = checkout.CheckoutState()
        self.checkout.session_id = self.id


class Hooks(checkout.Hooks):
    pass


def setup():
    client = FakeClient()
    accounts.set_grpc_client(client)
    checkout.hooks = Hooks()
    checkout.accounts = accounts
    PLACED.clear()
    return client


ADDR = dict(street_address="1 Main St", city="Austin", state="TX", zip_code="73301", country="United States")


def test_orders_require_login_and_use_server_identity():
    setup()
    guest = Sess("")
    assert account_tools.list_orders(guest)["status"] == "login_required"
    assert account_tools.get_order(guest, OWN)["status"] == "login_required"
    signed = Sess("tok-signed-in")
    r = account_tools.list_orders(signed)
    assert r["count"] == 1 and r["orders"][0]["order_id"] == OWN and "note" in r, r
    assert account_tools.get_order(signed, OWN)["order_id"] == OWN
    # nothing in the tool's schema lets the model pick a customer
    spec = {d["function"]["name"]: d["function"] for d in incident.tool_definitions()}
    assert set(spec["list_orders"]["parameters"]["properties"]) == {"limit"}
    assert set(spec["get_order"]["parameters"]["properties"]) == {"order_id"}


def test_other_customers_and_malformed_ids_are_not_found():
    setup()
    s = Sess("tok-signed-in")
    assert account_tools.get_order(s, OTHER)["status"] == "not_found"
    assert account_tools.get_order(s, "'; DROP TABLE orders;--")["status"] == "not_found"
    assert account_tools.get_order(s, "")["status"] == "not_found"


def test_outage_is_a_tool_error_not_a_crash():
    client = setup()
    client.unavailable = True
    s = Sess("tok-signed-in")
    assert account_tools.list_orders(s)["grpc_status"] == "UNAVAILABLE"
    assert account_tools.get_order(s, OWN)["grpc_status"] == "UNAVAILABLE"


def test_no_address_email_or_phone_in_order_results():
    setup()
    blob = json.dumps([account_tools.list_orders(Sess("tok-signed-in")), account_tools.get_order(Sess("tok-signed-in"), OWN)])
    for needle in ("street", "address", "@", "phone", "zip"):
        assert needle not in blob.lower(), needle


def _to_review(s, pm):
    checkout.start_checkout(s)
    checkout.continue_as_guest(s, email="a@example.com", name="A")
    checkout.set_shipping_address(s, **ADDR)
    r = checkout.set_payment(s, payment_method_id=pm)
    checkout.set_currency(s, currency_code="USD")
    checkout.review_order(s)
    s.turns += 1
    checkout.confirm_order(s)
    return r


def test_saved_methods_listed_and_charged_by_reference():
    setup()
    s = Sess("tok-signed-in")
    lst = checkout.list_payment_methods(s)
    assert [m["id"] for m in lst["payment_methods"]] == [PM_ID] and lst["payment_methods"][0]["saved"], lst
    assert lst["payment_methods"][0]["label"] == "Visa ending 4242"
    r = _to_review(s, PM_ID)
    assert r["status"] == "payment_selected" and r["payment_method"] == "Visa ending 4242", r
    out = checkout.place_order(s)
    assert out.get("order_id") == "ord-1", out
    call = PLACED[-1]
    assert call["pm"] == PM_ID and call["card"] is None and call["token"] == "tok-signed-in", call
    assert call["key"].startswith("asst:")


def test_guests_still_use_the_demo_catalog_and_card_form():
    setup()
    s = Sess("")
    lst = checkout.list_payment_methods(s)
    assert {m["id"] for m in lst["payment_methods"]} == set(accounts.PAYMENT_METHODS) and not any(m.get("saved") for m in lst["payment_methods"])
    _to_review(s, "pm_demo_visa")
    checkout.place_order(s)
    assert PLACED[-1]["pm"] == "" and PLACED[-1]["card"] == accounts.card_number("pm_demo_visa")


def test_unknown_saved_id_is_refused():
    setup()
    s = Sess("tok-signed-in")
    checkout.start_checkout(s)
    r = checkout.set_payment(s, payment_method_id="bbbbbbbb-2222-4333-8444-555555555555")
    assert r["status"] == "needs_info", r


def test_identity_attrs_have_no_pii_or_raw_session_id():
    a = tel.identity_attrs("browser-session-abc", OWN)
    assert a["app.customer.id"] == OWN and a["app.customer.authenticated"] is True and len(a["app.session.ref"]) == 16
    assert "browser-session-abc" not in json.dumps(a)
    g = tel.identity_attrs("browser-session-abc", "not-a-uuid-it-is-a-session-id")
    assert "app.customer.id" not in g and g["app.customer.authenticated"] is False
    assert not ({"app.session.id", "app.customer.email", "app.customer.name"} & (set(a) | set(g)))
    assert tel.opaque_fallback("x") == tel.opaque_fallback("x") != tel.opaque_fallback("y")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        t()
        print("PASS", t.__name__)
    print(f"{len(tests)} passed")
