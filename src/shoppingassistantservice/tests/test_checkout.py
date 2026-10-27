"""Unit tests for the checkout state machine, accounts and the v1/v2 prompt contract.

Stdlib only: `backends` (gRPC) is replaced by a stub, Redis by an in-memory fake.
Run from src/shoppingassistantservice:   python tests/test_checkout.py
"""
import json
import os
import sys
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

# --- stub backends (no grpc) -------------------------------------------------------
CART = {"items": [{"product_id": "P1", "name": "Mug", "quantity": 2, "unit_price_usd": 8.0}], "total": 16.0}
ORDER = {"fail": None, "calls": []}


def _cart_contents(user_id):
    return list(CART["items"]), CART["total"]


def _place_order(user_id, currency, email, address, card):
    ORDER["calls"].append({"currency": currency, "email": email, "card_len": len(card)})
    if ORDER["fail"]:
        return {"error": f"INTERNAL: {ORDER['fail']}", "grpc_status": "INTERNAL"}
    return {"order_id": "ord-1", "shipping_tracking_id": "trk", "items": []}


stub = types.ModuleType("backends")
stub.cart_contents, stub.place_order, stub.TOOLS = _cart_contents, _place_order, {}
sys.modules["backends"] = stub

import pgtest  # noqa: E402,F401
import accounts  # noqa: E402
import checkout  # noqa: E402
import config  # noqa: E402
import incident  # noqa: E402


class Rec(checkout.Hooks):
    def __init__(self):
        self.stages, self.terminals, self.events = [], [], []

    def stage(self, st, stage, prefilled):
        self.stages.append((stage, prefilled))

    def terminal(self, st, outcome, error_type=None):
        self.terminals.append((outcome, error_type))

    def event(self, name, st, **attrs):
        self.events.append((name, attrs))


class Sess:
    def __init__(self, sid="s1"):
        self.id, self.turns = sid, 0
        self.checkout = checkout.CheckoutState()
        self.checkout.session_id = sid


_REPO = {}


def fresh():
    if "r" not in _REPO:
        _REPO["r"] = pgtest.new_repo()
    pgtest.reset_data(_REPO["r"])
    accounts.store = accounts.AccountStore(pgtest.FakeRedis(), repo=_REPO["r"])
    checkout.accounts = accounts
    rec = Rec()
    checkout.hooks = rec
    ORDER.update(fail=None, calls=[])
    return Sess(), rec


ADDR = dict(street_address="1 Main St", city="Austin", state="TX", zip_code="73301", country="United States")
results_seen = []


def call(fn, s, **kw):
    r = fn(s, **kw)
    results_seen.append(json.dumps(r, default=str))
    return r


def guest_to_review(s):
    call(checkout.start_checkout, s)
    call(checkout.continue_as_guest, s, email="a@example.com", name="A")
    call(checkout.set_shipping_address, s, **ADDR)
    call(checkout.set_payment, s, payment_method_id="pm_demo_visa")
    call(checkout.set_currency, s, currency_code="JPY")
    return call(checkout.review_order, s)


def test_missing_fields_and_refusal():
    s, rec = fresh()
    r = call(checkout.place_order, s)
    assert r["status"] == "needs_info" and not ORDER["calls"], r
    call(checkout.start_checkout, s)
    assert s.checkout.snapshot()["stage"] == "identify"
    call(checkout.continue_as_guest, s, email="bad")
    assert s.checkout.snapshot()["stage"] == "identify"  # invalid email rejected
    guest_to_review(s)
    assert call(checkout.place_order, s)["status"] == "needs_info"  # reviewed but not confirmed


def test_confirmation_needs_a_later_turn():
    s, rec = fresh()
    guest_to_review(s)
    assert call(checkout.confirm_order, s)["status"] == "needs_info"  # same turn
    s.turns += 1
    assert call(checkout.confirm_order, s)["status"] == "confirmed"
    r = call(checkout.place_order, s)
    assert r["order_id"] == "ord-1" and ORDER["calls"][0]["currency"] == "JPY"
    assert rec.terminals == [("completed", None)]


def test_cart_change_and_currency_change_invalidate_confirmation():
    s, rec = fresh()
    guest_to_review(s)
    s.turns += 1
    call(checkout.confirm_order, s)
    CART["items"].append({"product_id": "P2", "name": "Watch", "quantity": 1, "unit_price_usd": 9.0})
    assert call(checkout.place_order, s)["status"] == "needs_info"
    CART["items"].pop()
    s2, _ = fresh()
    guest_to_review(s2)
    s2.turns += 1
    call(checkout.set_currency, s2, currency_code="EUR")
    assert not s2.checkout.confirmed and s2.checkout.reviewed_turn is None


def test_funnel_counts_each_stage_once_and_terminal_once_per_attempt():
    s, rec = fresh()
    guest_to_review(s)
    call(checkout.set_shipping_address, s, **ADDR)      # repeats must not re-count
    call(checkout.set_shipping_address, s, **ADDR)
    s.turns += 1
    call(checkout.confirm_order, s)
    ORDER["fail"] = "failed to convert price"
    assert "error" in call(checkout.place_order, s)
    assert rec.terminals == [("failed", "INTERNAL")]
    ORDER["fail"] = None
    assert "order_id" in call(checkout.place_order, s)   # retry
    assert rec.terminals == [("failed", "INTERNAL"), ("completed", None)]
    names = [x[0] for x in rec.stages]
    assert names == ["identify", "shipping", "payment", "currency", "confirm", "placing"], names
    assert checkout.sweep_abandoned([s], now=1e12) == 0  # finished flows are never abandoned


def test_abandonment_once():
    s, rec = fresh()
    call(checkout.start_checkout, s)
    call(checkout.continue_as_guest, s, email="a@example.com")
    assert checkout.sweep_abandoned([s], now=0) == 0     # not idle long enough
    assert checkout.sweep_abandoned([s], now=1e12) == 1
    assert checkout.sweep_abandoned([s], now=1e12) == 0  # exactly once
    assert rec.terminals == [("abandoned", "shipping")]


def test_accounts_new_then_returning_prefills():
    s, rec = fresh()
    call(checkout.start_checkout, s)
    assert call(checkout.lookup_account, s, email="r@example.com")["found"] is False
    r = call(checkout.create_account, s, first_name="R", last_name="Shopper", email="r@example.com", **ADDR)
    assert r["status"] == "account_created" and s.checkout.customer_type == "new"
    call(checkout.set_payment, s, payment_method_id="pm_demo_mastercard")
    # same session repeating the request resumes its own pending account (idempotent)
    assert call(checkout.create_account, s, first_name="R", last_name="Shopper", email="R@example.com", **ADDR)["status"] == "account_created"
    # a different browser session must not take over the pending account
    assert call(checkout.create_account, Sess("other"), first_name="R", last_name="Shopper", email="r@example.com", **ADDR)["status"] == "account_pending_elsewhere"

    s2 = Sess("s2")
    rec.stages.clear()
    r = call(checkout.lookup_account, s2, email="r@example.com")
    assert r["found"] and s2.checkout.customer_type == "returning"
    assert s2.checkout.address and s2.checkout.payment_method_id == "pm_demo_mastercard"
    assert s2.checkout.snapshot()["stage"] == "currency"
    pre = {st: p for st, p in rec.stages}
    assert pre == {"identify": False, "shipping": True, "payment": True, "currency": False}, pre


def test_activation_is_session_bound_and_single_use():
    fresh()
    st = accounts.store
    res = st.create_pending("session-a", "Demo Shopper", "demo@example.com", {k: "x" * 3 for k in checkout.ADDRESS_FIELDS})
    assert res["outcome"] == "created" and res["handoff"]
    assert st.activation_status("session-b") == {"pending": False}          # other session sees nothing
    assert st.claim("session-b", "Passw0rdOK") == (None, None)               # and cannot claim
    acct, token = st.claim("session-a", "Passw0rdOK")
    assert acct and token and acct["email"] == "demo@example.com"
    assert st.claim("session-a", "Passw0rdOK") == (None, None)               # single use
    assert st.authenticate("demo@example.com", "Passw0rdOK")


    blob = "\n".join(results_seen)
    for card in ("4432", "4111", "5555", "6152"):
        assert card not in blob, card


def _strip_currency(defs):
    out = json.loads(json.dumps(defs))
    for d in out:
        props = d["function"]["parameters"]["properties"]
        if "currency_code" in props:
            props["currency_code"].pop("description", None)
    return out


def test_v1_v2_differ_only_in_currency_handling():
    res = {}
    for v in ("v1", "v2"):
        config.PROMPT_VERSION = v
        res[v] = (incident.system_prompt(), incident.tool_definitions())
    p1, t1 = res["v1"]
    p2, t2 = res["v2"]
    assert p1 != p2 and p2.replace(incident._CURRENCY_STYLE["v2"], "") == p1
    assert t1 != t2 and _strip_currency(t1) == _strip_currency(t2)
    names = {d["function"]["name"] for d in t1}
    for n in ("start_checkout", "lookup_account", "create_account", "set_payment", "set_currency", "place_order"):
        assert n in names, n
    config.PROMPT_VERSION = "v1"


def test_account_requires_first_and_last_name():
    # Chat path: every missing field is named back so the assistant can ask for it.
    s, _ = fresh()
    call(checkout.start_checkout, s)
    r = call(checkout.create_account, s, first_name="Ada", last_name="", email="ada@example.com",
             street_address="1 Main St", city="Austin", state="TX", zip_code="", country="United States")
    assert r["status"] == "needs_info" and s.checkout.customer_type != "new"
    assert r["missing_fields"] == ["last name", "postal code"], r["missing_fields"]
    assert "last name" in r["hint"] and "first name" in r["hint"]
    r = call(checkout.create_account, s, first_name="Ada", last_name="Lovelace", email="ada@example.com", **ADDR)
    assert r["status"] == "account_created" and r["name"] == "Ada Lovelace"
    # Web registration: both names required, combined into the stored display name.
    _, _, errors = accounts.validate_registration({"email": "w@example.com", "password": "Sup3rPassw0rd!",
                                                   "first_name": "", "last_name": " ", **ADDR})
    assert {"first_name", "last_name"} <= set(errors)
    profile, _, errors = accounts.validate_registration({"email": "w@example.com", "password": "Sup3rPassw0rd!",
                                                         "first_name": "Web", "last_name": "Shopper", **ADDR})
    assert not errors and profile["name"] == "Web Shopper"
    assert accounts.split_name("Mary Ann Smith") == ("Mary", "Ann Smith")
    # Tool schema: the model cannot call create_account without both names.
    spec = {d["function"]["name"]: d["function"] for d in incident.tool_definitions()}["create_account"]
    assert {"first_name", "last_name"} <= set(spec["parameters"]["required"]) and "name" not in spec["parameters"]["properties"]
    assert "first name, last name" in incident.system_prompt()


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    # test_no_card_data_in_tool_results inspects results gathered by the others, so run it last.
    tests.sort(key=lambda f: f.__name__ == "test_no_card_data_in_tool_results")
    for t in tests:
        t()
        print("PASS", t.__name__)
    print(f"{len(tests)} passed")
