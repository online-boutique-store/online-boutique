"""Typed schema for every app.* telemetry attribute this service may emit.

Single choke point: telemetry.set_attrs() coerces values through this schema when attributes are
set, and the export-time sanitizer drops any app.* attribute that is not in the schema or whose
value does not satisfy it. IDs therefore cannot carry free text, and unknown app.* keys (typos,
future code, third parties) never leave the process. Fail closed.
"""
import re

_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_REF = re.compile(r"^[0-9a-f]{16}$")
_SAFE_ID = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
_CODE = re.compile(r"^[a-z0-9_.]{1,48}$")
_CURRENCY = re.compile(r"^[A-Z]{3}$")


def _enum(*values):
    return ("enum", frozenset(values))


ACTIONS = ("register", "activate", "login", "logout", "profile_update", "address_create", "address_update",
           "address_delete", "address_default", "payment_add", "payment_delete", "payment_default",
           "orders_list", "order_view", "dashboard_view", "claim_guest_orders", "validate", "session_resolve",
           "assistant_signup", "profile_get", "address_list", "payment_list", "payment_resolve")
CHECKOUT_STATES = ("STARTED", "PAYMENT_ATTEMPTING", "PAYMENT_CHARGED", "PAYMENT_DECLINED", "PAYMENT_UNCERTAIN",
                   "SHIPPING_ATTEMPTING", "SHIPPED", "COMPLETED", "FAILED_PRE_PAYMENT")

SCHEMA = {
    # customer / account
    "app.customer.id": ("regex", _UUID),
    "app.customer.authenticated": ("bool",),
    "app.customer.type": _enum("authenticated", "guest", "anonymous"),
    "app.customer.profile_complete": ("bool",),
    "app.account.action": _enum(*ACTIONS),
    "app.account.created_via": _enum("web", "assistant"),
    "app.account.profile_version": ("int", 1, 2),
    "app.account.outcome": ("regex", _CODE),
    "app.account.guest_claim": _enum("failed"),
    "app.account.guest_orders_claimed": ("int", 0, 100000),
    "app.account.activation.outcome": ("regex", _CODE),
    "app.account.handoff.outcome": ("regex", _CODE),
    "app.account.db.outcome": ("regex", _CODE),
    "app.account.db.instance_changed": ("bool",),
    "app.account.migration.outcome": ("regex", _CODE),
    "app.account.migration.version": ("regex", _CODE),
    "app.account.schema.validation": ("regex", _CODE),
    "app.account.address_count": ("int", 0, 100),
    "app.account.payment_method_count": ("int", 0, 100),
    "app.account.service_caller": ("regex", _SAFE_ID),
    # validation
    "app.validation.fields": ("strlist", re.compile(r"^[a-z0-9_]{1,32}$")),
    "app.validation.codes": ("strlist", re.compile(r"^[a-z0-9_]{1,48}$")),
    # session / prompt
    "app.session.ref": ("regex", _REF),
    "app.prompt.version": _enum("v1", "v2"),
    # orders
    "app.order.id": ("regex", _UUID),
    "app.order.status": _enum("confirmed", "shipped", "delivered", "cancelled"),
    "app.order.currency": ("regex", _CURRENCY),
    "app.order.total_minor_units": ("int", 0, 10 ** 15),
    "app.order.item_count": ("int", 0, 10000),
    "app.order.channel": _enum("web", "assistant"),
    "app.order.persistence_status": _enum("recorded", "history_unrecorded", "pending", "dead"),
    "app.order.outbox_status": _enum("accepted", "consumed", "dead", "unconfirmed"),
    "app.order.retry_count": ("int", 0, 1000),
    "app.order.persistence_latency_ms": ("int", 0, 10 ** 9),
    "app.order.record_phase": _enum("intent", "completed"),
    # checkout idempotency
    "app.checkout.idempotency_status": _enum("new", "replayed", "in_progress", "resumed", "conflict", "rejected"),
    "app.checkout.state": _enum(*CHECKOUT_STATES),
    "app.checkout.resume_from": _enum(*CHECKOUT_STATES),
    "app.checkout.lease_epoch": ("int", 0, 10 ** 6),
    # products / cart
    "app.product.id": ("regex", _SAFE_ID),
    "app.product.category": ("regex", _SAFE_ID),
    "app.cart.item_count": ("int", 0, 10000),
    # payments / shipping
    "app.payment.method_type": _enum("demo_card_saved", "card_form"),
    "app.payment.status": _enum("tokenized", "charged", "declined", "uncertain", "failed"),
    "app.payment.provider": _enum("demo-psp"),
    "app.shipping.status": _enum("quoted", "pending", "shipped", "failed"),
    "app.shipping.method": ("regex", _SAFE_ID),
    "app.shipping.is_po_box": ("bool",),
}


def coerce(key, value):
    """Return the value if it satisfies the schema for `key`, else raise ValueError."""
    spec = SCHEMA.get(key)
    if spec is None:
        raise ValueError(f"unknown attribute {key}")
    kind = spec[0]
    if kind == "bool":
        if isinstance(value, bool):
            return value
    elif kind == "int":
        if isinstance(value, int) and not isinstance(value, bool) and spec[1] <= value <= spec[2]:
            return value
    elif kind == "enum":
        if isinstance(value, str) and value in spec[1]:
            return value
    elif kind == "regex":
        if isinstance(value, str) and spec[1].match(value):
            return value
    elif kind == "strlist":
        if isinstance(value, (list, tuple)) and len(value) <= 32 and all(isinstance(v, str) and spec[1].match(v) for v in value):
            return list(value)
    raise ValueError(f"invalid value for {key}")


def minor_units(units, nanos, currency):
    """Money (units + nanos) -> integer minor units; JPY has no minor unit, everything else 2."""
    exponent = 0 if currency == "JPY" else 2
    scaled = (int(units) * 10 ** 9 + int(nanos)) * (10 ** exponent)
    return scaled // 10 ** 9
