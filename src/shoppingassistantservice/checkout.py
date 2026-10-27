"""Guided checkout state machine.

The model stays conversational, but WHAT is missing and WHETHER an order may be
placed is decided here, in code, from structured fields - never from the model's
wording. Every checkout tool returns a `checkout` block (stage, missing,
next_step) that the model follows, and place_order is refused unless every field
is present and the customer confirmed in a LATER turn than the order summary.

Telemetry goes through `hooks` (wired to the OpenTelemetry API in agent.py) so
this module stays testable without an SDK:
  hooks.span(name, **attrs)         child span around account/Redis work
  hooks.event(name, st, **attrs)    structured log + span event
  hooks.stage(st, stage, prefilled) funnel: once per stage per checkout flow
  hooks.terminal(st, outcome, ...)  funnel: exactly one per attempt
"""
import contextlib
import re
import time
import uuid

import accounts
import telemetry_sanitize
import backends
import config

STAGES = ["identify", "shipping", "payment", "currency", "confirm", "placing"]
_EMAIL = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")
ADDRESS_FIELDS = ("street_address", "city", "state", "zip_code", "country")


class Hooks:
    @contextlib.contextmanager
    def span(self, name, **attrs):
        yield None

    def event(self, name, st, **attrs):
        pass

    def stage(self, st, stage, prefilled):
        pass

    def terminal(self, st, outcome, error_type=None):
        pass


hooks = Hooks()


class CheckoutState:
    def __init__(self):
        self.reset()
        self.last_activity = time.monotonic()

    def reset(self):
        self.flow_id = uuid.uuid4().hex[:10]
        self.started = False
        self.attempt = 0                  # place_order executions this flow
        self.attempt_open = False         # a terminal outcome is still owed
        self.reached = set()              # funnel stages already counted this flow
        self.customer_type = "unknown"    # new | returning | guest
        self.email = None
        self.email_hash = None
        self.name = None
        self.address = None
        self.payment_method_id = None
        self.saved_payment_labels = {}   # id -> label for the signed-in customer's saved methods
        self.customer_id = None          # Postgres customer UUID (signed-in sessions only)
        self.currency = None
        self.currency_injected = False   # the stored currency came from synthetic fault injection
        self.reviewed_turn = None
        self.review_fingerprint = None
        self.confirmed = False
        self.done = False
        self.idempotency_key = None      # reused across retries of the same reviewed order
        self.idempotency_basis = None
        self.stage = "identify"

    @property
    def checkout_id(self):
        return f"{self.flow_id}-{max(self.attempt, 1)}"

    def touch(self):
        self.last_activity = time.monotonic()

    def set_identity(self, email, name, customer_type):
        self.email, self.name, self.customer_type = email, name, customer_type
        self.email_hash = accounts.email_hash(email)

    def missing(self):
        out = []
        if not self.email:
            out.append("customer identity")
        if not self.address:
            out.append("shipping address")
        if not self.payment_method_id:
            out.append("payment method")
        if not self.currency:
            out.append("currency")
        return out

    def current_stage(self):
        if not self.email:
            return "identify"
        if not self.address:
            return "shipping"
        if not self.payment_method_id:
            return "payment"
        if not self.currency:
            return "currency"
        if not self.confirmed:
            return "confirm"
        return "placing"

    def next_step(self):
        if self.done:
            return "Order placed. Tell the customer the order id and shipping details."
        return {
            "identify": "Ask for the customer's email. Call lookup_account; if not found offer create_account "
                        "or continue_as_guest.",
            "shipping": "Ask for the shipping address (street, city, state, zip, country), then call "
                        "set_shipping_address.",
            "payment": "Call list_payment_methods, offer the labels, and call set_payment with the one the "
                       "customer picks. Never ask for card numbers.",
            "currency": "Ask which currency the customer wants to pay in, then call set_currency.",
            "confirm": ("Call review_order and read the summary to the customer, then wait for their reply. "
                        "Only after they say yes in a new message call confirm_order."
                        if self.reviewed_turn is None else
                        "If the customer clearly said yes to the summary call confirm_order; otherwise ask."),
            "placing": "Call place_order.",
        }[self.current_stage()]

    def snapshot(self):
        self.stage = "done" if self.done else self.current_stage()
        return {"stage": self.stage, "missing": self.missing(), "next_step": self.next_step(),
                "customer_type": self.customer_type, "confirmed": self.confirmed,
                "checkout_id": self.checkout_id}

    def advance(self, prefilled=()):
        """Mark every stage up to the first unmet one as reached (once per flow)."""
        idx = STAGES.index(self.current_stage())
        for s in STAGES[: idx + 1]:
            if s not in self.reached:
                self.reached.add(s)
                hooks.stage(self, s, s in prefilled)


def _need(st, why):
    hooks.event("checkout.needs_info", st, reason=why)
    return {"status": "needs_info", "reason": why, "checkout": st.snapshot()}


def _begin(st):
    if st.started and not st.done:
        return
    st.reset()
    st.started = True
    st.attempt_open = True
    hooks.event("checkout.started", st)
    st.advance()


def _valid_address(a):
    return all(str(a.get(k) or "").strip() for k in ADDRESS_FIELDS)


def _clean_address(args):
    address = {k: str(args.get(k) or "").strip() for k in ADDRESS_FIELDS}
    telemetry_sanitize.register_address(address)
    return address


def _grpc():
    return config.ACCOUNT_BACKEND == "grpc"


_GRPC_LABELS = {"first_name": "first name", "last_name": "last name", "email": "email address", "phone": "phone number",
                "address_line1": "street address", "address_line2": "apartment or suite", "city": "city", "state": "state",
                "zip": "ZIP code", "country": "country"}
_GRPC_ORDER = list(_GRPC_LABELS)


def _reg_fields(first_name="", last_name="", email="", phone="", street_address="", address_line2="", city="", state="",
                zip_code="", country=""):
    return {"first_name": first_name, "last_name": last_name, "email": email, "phone": phone,
            "address_line1": street_address, "address_line2": address_line2, "city": city, "state": state,
            "zip": zip_code, "country": country}


def _register_pii(fields):
    """Tell the telemetry sanitizer about this customer's exact values so any echo is scrubbed."""
    telemetry_sanitize.register_sensitive(fields.get("first_name"), fields.get("last_name"), min_len=3)
    telemetry_sanitize.register_sensitive(fields.get("email"), fields.get("phone"), fields.get("address_line1"),
                                          fields.get("address_line2"), fields.get("city"), fields.get("zip"))


def _legacy_address(n):
    """Server-normalized address -> the assistant's internal address shape."""
    street = n["line1"] + (f", {n['line2']}" if n.get("line2") else "")
    return {"street_address": street, "city": n["city"], "state": n["state"], "zip_code": n["zip"], "country": n["country"] or "US"}


def _need_fields(st, errors):
    """Needs-info result from server field errors (codes + contract messages; never the submitted values)."""
    ordered = [f for f in _GRPC_ORDER if f in errors]
    labels = [_GRPC_LABELS[f] for f in ordered]
    out = _need(st, "missing or invalid: " + ", ".join(labels))
    out["missing_fields"] = labels
    out["field_errors"] = {_GRPC_LABELS[f]: errors[f]["message"] for f in ordered}
    out["hint"] = ("Tell the customer exactly which of these are still needed or need fixing: " + ", ".join(labels)
                   + ". Use the messages in field_errors. Required: first name, last name, email, phone number, "
                   "street address, city, state and ZIP code (US addresses only; apartment or suite is optional).")
    return out


def _grpc_unavailable(st, session, what):
    return {"error": f"account service unavailable: {what}", "hint": "offer to continue as guest",
            "checkout": st.snapshot()}


def _cart_fingerprint(user_id):
    items, total = backends.cart_contents(user_id)
    return tuple(sorted((i["product_id"], i["quantity"]) for i in items)), items, total


def _attach(st, result):
    result["checkout"] = st.snapshot()
    return result


def start_checkout(session):
    st = session.checkout
    st.touch()
    try:
        fp, items, total = _cart_fingerprint(session.id)
    except Exception as e:  # noqa: BLE001
        return {"error": f"cart lookup failed: {type(e).__name__}"}
    if not items:
        return {"status": "cart_empty", "hint": "Help the customer add items before checking out."}
    _begin(st)
    return _attach(st, {"status": "started", "cart_total_usd": total, "item_count": len(items)})


def _lookup_account_grpc(session, email):
    st = session.checkout
    st.touch()
    _begin(st)
    ident = getattr(session, "customer_account", None)
    if not ident or not ident.get("customer_id"):
        hooks.event("account.lookup", st, found=False, outcome="not_signed_in")
        # Never confirm whether an email has an account: only a signed-in session reveals saved details.
        return _attach(st, {"found": False, "signed_in": False,
                            "hint": "The customer is not signed in. Offer to log in on the storefront, create_account, or "
                                    "continue_as_guest. Do not say whether an account exists for an email address."})
    st.set_identity(ident["email"], ident.get("name"), "returning")
    st.address = ident.get("address")
    prefilled = {"identify"} | ({"shipping"} if st.address else set())
    hooks.event("account.lookup", st, found=True, outcome="found", returning=True, prefilled_address=bool(st.address),
                prefilled_payment=False)
    st.advance(prefilled)
    out = {"found": True, "signed_in": True, "name": st.name}
    if st.address:
        out["address_on_file"] = st.address
    return _attach(st, out)


def lookup_account(session, email):
    if _grpc():
        return _lookup_account_grpc(session, email)
    st = session.checkout
    st.touch()
    email = accounts.normalize_email(email)
    if not _EMAIL.match(email):
        return _need(st, "that does not look like a valid email address")
    _begin(st)
    with hooks.span("account.lookup") as sp:
        try:
            acct = accounts.store.get(email)
        except Exception as e:  # noqa: BLE001
            if sp is not None:
                sp.record_exception(e)
            hooks.event("account.lookup", st, found=False, outcome="error", error_type=type(e).__name__)
            ident = getattr(session, "customer_account", None)
            if ident and ident.get("email"):
                # Logged-in customer: keep who they are; do NOT fall back to guest on our own.
                st.set_identity(ident["email"], ident.get("name"), "returning")
                return _attach(st, {"status": "account_unavailable",
                                    "hint": "Tell the customer their saved account details are temporarily "
                                            "unavailable. They may wait and retry, or explicitly choose to "
                                            "check out as a guest."})
            return {"error": f"account service unavailable: {type(e).__name__}",
                    "hint": "offer to continue as guest", "checkout": st.snapshot()}
        if sp is not None:
            sp.set_attribute("app.account.found", bool(acct))
    st.email_hash = accounts.email_hash(email)
    if not acct:
        hooks.event("account.lookup", st, found=False, outcome="not_found")
        return _attach(st, {"found": False, "hint": "offer create_account or continue_as_guest"})
    st.set_identity(acct["email"], acct.get("name"), "returning")
    st.address = acct.get("address")
    pm = acct.get("default_payment_method_id")
    st.payment_method_id = pm if pm in accounts.PAYMENT_METHODS else None
    prefilled = {"identify"}
    if st.address:
        prefilled.add("shipping")
    if st.payment_method_id:
        prefilled.add("payment")
    hooks.event("account.lookup", st, found=True, outcome="found", returning=True,
                prefilled_address=bool(st.address), prefilled_payment=bool(st.payment_method_id))
    st.advance(prefilled)
    out = {"found": True, "name": st.name, "address_on_file": st.address}
    if st.payment_method_id:
        out["payment_method_on_file"] = accounts.PAYMENT_METHODS[st.payment_method_id]["label"]
    return _attach(st, out)


_FIELD_LABELS = {"first_name": "first name", "last_name": "last name", "email": "email address",
                 "street_address": "street address", "city": "city", "state": "state or region",
                 "zip_code": "postal code", "country": "country"}


def _create_account_grpc(session, fields):
    st = session.checkout
    st.touch()
    _register_pii(fields)
    client = accounts.grpc_client()
    try:
        errors, n = client.validate(fields)
    except Exception as e:  # noqa: BLE001
        hooks.event("account.create", st, outcome="error", error_type=type(e).__name__)
        return _grpc_unavailable(st, session, type(e).__name__)
    errors = {k: v for k, v in errors.items() if not k.startswith("password") and k != "confirm_password"}
    if errors:
        return _need_fields(st, errors)
    telemetry_sanitize.register_sensitive(n["phone"], n["email"])
    _begin(st)
    with hooks.span("account.create") as sp:
        try:
            res = client.begin_signup(getattr(session, "id", ""), fields)
        except Exception as e:  # noqa: BLE001
            if sp is not None:
                sp.record_exception(e)
            hooks.event("account.create", st, outcome="error", error_type=type(e).__name__)
            if type(e).__name__ == "ValidationFailed":
                return _need_fields(st, e.fields)
            return _grpc_unavailable(st, session, type(e).__name__)
        outcome = res["outcome"]
        if sp is not None:
            sp.set_attribute("app.account.activation.outcome", outcome)
    if outcome == "exists":
        hooks.event("account.create", st, outcome="exists")
        return _attach(st, {"status": "account_exists", "hint": "An account with that email may already exist. Ask the customer "
                                                                "to log in on the storefront, or continue as a guest."})
    if outcome == "pending_elsewhere":
        hooks.event("account.create", st, outcome="pending_elsewhere")
        return _attach(st, {"status": "account_pending_elsewhere",
                            "hint": "An account setup for this email is already in progress. Offer to continue as a guest or "
                                    "to try again later."})
    st.set_identity(n["email"], f"{n['first_name']} {n['last_name']}".strip(), "new")
    st.address = _legacy_address(n)
    session.pending_activation = bool(res["handoff"])
    hooks.event("account.create", st, outcome="created", activation_outcome=outcome, activation_pending=bool(res["handoff"]),
                **{"app.account.handoff.outcome": "ok" if res["handoff"] else "failed"})
    st.advance()
    hint = ("The storefront will show Complete Account Setup. Never ask for a password in chat."
            if res["handoff"] else
            "The account was created but the setup link is temporarily unavailable. Continue checkout; "
            "never ask for a password in chat.")
    return _attach(st, {"status": "account_created", "name": st.name, "complete_setup": bool(res["handoff"]), "hint": hint})


def create_account(session, first_name, last_name, email, street_address, city, state, zip_code, country="US",
                   phone="", address_line2=""):
    if _grpc():
        return _create_account_grpc(session, _reg_fields(first_name, last_name, email, phone, street_address,
                                                         address_line2, city, state, zip_code, country))
    st = session.checkout
    st.touch()
    email = accounts.normalize_email(email)
    address = _clean_address(dict(street_address=street_address, city=city, state=state,
                                  zip_code=zip_code, country=country))
    name, name_errors = accounts.validate_person_name({"first_name": first_name, "last_name": last_name})
    _, addr_errors = accounts.validate_address(address)
    missing = [k for k in name_errors] + ([] if _EMAIL.match(email) else ["email"]) + [k for k in addr_errors]
    if missing:
        labels = [_FIELD_LABELS.get(k, k) for k in _FIELD_LABELS if k in missing]
        out = _need(st, "missing or invalid: " + ", ".join(labels))
        out["missing_fields"] = labels
        out["hint"] = ("Tell the customer exactly which of these are still needed: " + ", ".join(labels)
                       + ". All of first name, last name, email, street address, city, state or region, "
                       "postal code and country are required.")
        return out
    _begin(st)
    with hooks.span("account.create") as sp:
        try:
            res = accounts.store.create_pending(getattr(session, "id", ""), name, email, address)
        except Exception as e:  # noqa: BLE001
            if sp is not None:
                sp.record_exception(e)
            hooks.event("account.create", st, outcome="error", error_type=type(e).__name__)
            return {"error": f"account service unavailable: {type(e).__name__}",
                    "hint": "offer to continue as guest", "checkout": st.snapshot()}
        outcome = res["outcome"]
        if sp is not None:
            sp.set_attribute("app.account.created", outcome in ("created", "resumed", "recreated_abandoned"))
            sp.set_attribute("app.account.activation.outcome", outcome)
    if outcome == "exists":
        hooks.event("account.create", st, outcome="exists")
        return _attach(st, {"status": "account_exists", "hint": "call lookup_account with this email"})
    if outcome == "pending_elsewhere":
        hooks.event("account.create", st, outcome="pending_elsewhere")
        return _attach(st, {"status": "account_pending_elsewhere",
                            "hint": "An account setup for this email is already in progress. Offer to "
                                    "continue as a guest or to try again later."})
    st.set_identity(email, name, "new")
    st.address = address
    session.pending_activation = bool(res["handoff"])
    hooks.event("account.create", st, outcome="created", activation_outcome=outcome,
                activation_pending=bool(res["handoff"]), **{"app.account.handoff.outcome": "ok" if res["handoff"] else "failed"})
    st.advance()
    hint = ("The storefront will show Complete Account Setup. Never ask for a password in chat."
            if res["handoff"] else
            "The account was created but the setup link is temporarily unavailable. Continue checkout; "
            "never ask for a password in chat.")
    return _attach(st, {"status": "account_created", "name": st.name,
                        "complete_setup": bool(res["handoff"]), "hint": hint})


def continue_as_guest(session, email, name=""):
    st = session.checkout
    st.touch()
    if _grpc():
        email = str(email or "").strip()
        try:
            errors, n = accounts.grpc_client().validate({"email": email}, only={"email"})
        except Exception as e:  # noqa: BLE001
            return _grpc_unavailable(st, session, type(e).__name__)
        if errors:
            return _need(st, errors["email"]["message"])
        email = n["email"]
    else:
        email = accounts.normalize_email(email)
        if not _EMAIL.match(email):
            return _need(st, "that does not look like a valid email address")
    _begin(st)
    st.set_identity(email, str(name or "").strip() or None, "guest")
    hooks.event("checkout.guest", st)
    st.advance()
    return _attach(st, {"status": "guest"})


def set_shipping_address(session, street_address, city, state, zip_code, country="US", address_line2=""):
    st = session.checkout
    st.touch()
    if _grpc():
        fields = _reg_fields(street_address=street_address, address_line2=address_line2, city=city, state=state,
                             zip_code=zip_code, country=country)
        _register_pii(fields)
        try:
            errors, n = accounts.grpc_client().validate(fields, only={"address_line1", "address_line2", "city", "state", "zip", "country"})
        except Exception as e:  # noqa: BLE001
            return _grpc_unavailable(st, session, type(e).__name__)
        if errors:
            return _need_fields(st, errors)
        _begin(st)
        st.address = _legacy_address(n)
        hooks.event("checkout.shipping_set", st, country=st.address["country"])
        st.advance()
        return _attach(st, {"status": "address_saved"})
    address = _clean_address(dict(street_address=street_address, city=city, state=state,
                                  zip_code=zip_code, country=country))
    if not _valid_address(address):
        return _need(st, "street address, city, state, zip code and country are all required")
    _begin(st)
    st.address = address
    hooks.event("checkout.shipping_set", st, country=address["country"])
    st.advance()
    return _attach(st, {"status": "address_saved"})


def list_payment_methods(session):
    session.checkout.touch()
    saved = _saved_methods(session)
    if saved:
        return {"payment_methods": [{"id": k, "label": v, "saved": True} for k, v in saved.items()],
                "note": "the customer's saved synthetic test cards; never ask for real card numbers"}
    return {"payment_methods": accounts.list_payment_methods(),
            "note": "synthetic test cards; never ask for real card numbers"}


def _saved_methods(session):
    """{id: label} of the signed-in customer's saved demo cards ({} for guests or when unavailable)."""
    tok = getattr(session, "account_token", "") or ""
    if not tok or config.ACCOUNT_BACKEND != "grpc":
        return {}
    try:
        methods = accounts.grpc_client().payment_methods(tok)
    except Exception:  # noqa: BLE001 - fall back to the demo catalog; never block checkout on this
        return {}
    return {m["id"]: f"{m['brand'].capitalize()} ending {m['last4']}" for m in methods}


def _pm_label(st):
    return st.saved_payment_labels.get(st.payment_method_id) or accounts.PAYMENT_METHODS[st.payment_method_id]["label"]


def set_payment(session, payment_method_id):
    st = session.checkout
    st.touch()
    saved = _saved_methods(session)
    if payment_method_id not in saved and payment_method_id not in accounts.PAYMENT_METHODS:
        return _need(st, "unknown payment_method_id; call list_payment_methods")
    _begin(st)
    st.saved_payment_labels = saved
    st.payment_method_id = payment_method_id
    hooks.event("checkout.payment_selected", st, payment_method_id=payment_method_id)
    st.advance()
    if payment_method_id not in saved and config.ACCOUNT_BACKEND != "grpc" and st.customer_type in ("new", "returning") and st.email:
        try:
            accounts.store.update(st.email, default_payment_method_id=payment_method_id)
        except Exception:  # noqa: BLE001 - saving the default is best effort
            pass
    return _attach(st, {"status": "payment_selected",
                        "payment_method": _pm_label(st)})


def set_currency(session, currency_code):
    """Stores the value EXACTLY as the model produced it (v1 expects an ISO code, v2
    the customer's wording); validity is only observed in telemetry, never repaired."""
    st = session.checkout
    st.touch()
    if not str(currency_code or "").strip():
        return _need(st, "a currency is required")
    _begin(st)
    if st.currency != currency_code:
        st.confirmed, st.reviewed_turn = False, None  # a changed currency invalidates the summary
    st.currency = currency_code
    hooks.event("checkout.currency_selected", st, currency_code=str(currency_code))
    st.advance()
    return _attach(st, {"status": "currency_set"})


def review_order(session):
    st = session.checkout
    st.touch()
    _begin(st)
    if st.missing():
        return _need(st, "cannot review yet: missing " + ", ".join(st.missing()))
    try:
        fp, items, total = _cart_fingerprint(session.id)
    except Exception as e:  # noqa: BLE001
        return {"error": f"cart lookup failed: {type(e).__name__}"}
    if not items:
        return {"status": "cart_empty"}
    st.reviewed_turn, st.review_fingerprint, st.confirmed = session.turns, fp, False
    hooks.event("checkout.review", st, value_usd=total)
    st.advance()
    return _attach(st, {"status": "review", "summary": {
        "items": items, "total_usd": total, "email": st.email,
        "ship_to": st.address, "payment_method": _pm_label(st),
        "currency": st.currency},
        "instruction": "Read this to the customer and ask them to confirm. Do not confirm for them."})


def confirm_order(session):
    st = session.checkout
    st.touch()
    if st.missing():
        return _need(st, "cannot confirm yet: missing " + ", ".join(st.missing()))
    if st.reviewed_turn is None:
        return _need(st, "call review_order first and show the summary to the customer")
    if session.turns <= st.reviewed_turn:
        return _need(st, "the customer has not replied to the summary yet; ask them and wait")
    st.confirmed = True
    hooks.event("checkout.confirmed", st)
    st.advance()
    return _attach(st, {"status": "confirmed"})


def place_order(session):
    st = session.checkout
    st.touch()
    if st.done:
        return _attach(st, {"status": "already_placed"})
    if st.missing() or not st.confirmed:
        return _need(st, "order not ready: " + (", ".join(st.missing()) or "customer has not confirmed"))
    try:
        fp, _, _ = _cart_fingerprint(session.id)
    except Exception as e:  # noqa: BLE001
        return {"error": f"cart lookup failed: {type(e).__name__}"}
    if fp != st.review_fingerprint:
        st.confirmed, st.reviewed_turn = False, None
        return _need(st, "the cart changed after the summary; call review_order again")
    st.attempt += 1
    st.attempt_open = True
    key = _idempotency_key(st, fp)
    saved = st.payment_method_id in st.saved_payment_labels
    result = backends.place_order(session.id, st.currency, st.email, st.address,
                                  None if saved else accounts.card_number(st.payment_method_id), idempotency_key=key,
                                  account_token=getattr(session, "account_token", "") or "",
                                  payment_method_id=st.payment_method_id if saved else "")
    if "error" in result:
        reason = result.get("reason", "")
        hooks.terminal(st, "failed", reason or result.get("grpc_status", "tool_error"))
        st.attempt_open = False
        if reason in ("payment_method_unavailable", "login_required"):
            st.payment_method_id, st.saved_payment_labels = None, {}
            result["hint"] = "That saved payment method can't be used. Call list_payment_methods and ask the customer to choose again."
        if reason in ("payment_uncertain", "checkout_in_progress", "order_queue_unavailable"):
            # Same key on the next try: checkout resolves the earlier attempt instead of charging again.
            result["retry_safe"] = True
        return _attach(st, result)
    st.done = True
    hooks.terminal(st, "completed")
    st.attempt_open = False
    return _attach(st, result)


def _idempotency_key(st, cart_fp):
    """One key per reviewed order. A changed cart, address, card or currency is a different order and
    gets a new key (reusing the old one would be an idempotency conflict at checkout)."""
    import hashlib
    basis = hashlib.sha256(repr((cart_fp, st.currency, st.payment_method_id, sorted((st.address or {}).items()),
                                 st.email)).encode()).hexdigest()
    if st.idempotency_key is None or st.idempotency_basis != basis:
        st.idempotency_key = f"asst:{st.flow_id}:{uuid.uuid4().hex[:16]}"
        st.idempotency_basis = basis
    return st.idempotency_key


TOOLS = {f.__name__: f for f in (
    start_checkout, lookup_account, create_account, continue_as_guest, set_shipping_address,
    list_payment_methods, set_payment, set_currency, review_order, confirm_order, place_order)}


def sweep_abandoned(sessions, now=None):
    """Emit `abandoned` once for flows that never reached an order attempt and went idle."""
    now = time.monotonic() if now is None else now
    n = 0
    for s in list(sessions):
        st = s.checkout
        if st.started and st.attempt_open and not st.done and \
                now - st.last_activity > config.CHECKOUT_ABANDON_AFTER_S:
            hooks.terminal(st, "abandoned", st.current_stage())
            st.attempt_open = False
            n += 1
    return n
