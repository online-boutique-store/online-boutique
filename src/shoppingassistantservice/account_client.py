"""gRPC client for accountservice (ACCOUNT_BACKEND=grpc).

The assistant never decides whether account details are valid and never reads Postgres: validation,
account creation, activation and customer data all come from accountservice. Session identity is
derived by the server from the opaque account-session token; this client never sends a customer id.
"""
import os

import time

import grpc
from google.rpc import error_details_pb2
from grpc_status import rpc_status

import account_pb2 as apb
import config
import demo_pb2 as dpb
import demo_pb2_grpc  # noqa: F401  (registers service descriptors)
import account_pb2_grpc as apbg

SERVICE_NAME = "shoppingassistantservice"
FIELD_ORDER = ["first_name", "last_name", "email", "phone", "address_line1", "address_line2", "city", "state", "zip", "country",
               "password", "confirm_password", "preferred_currency"]


class AccountUnavailable(Exception):
    """accountservice (or its database) is not reachable; reason is a short enum, never a message."""

    def __init__(self, reason="unreachable"):
        super().__init__(reason)
        self.reason = reason


class ValidationFailed(Exception):
    def __init__(self, fields):
        super().__init__("validation_failed")
        self.fields = fields          # {field: {"code", "message"}}


def _decode(err):
    st = rpc_status.from_call(err)
    fields, reason = {}, None
    for d in (st.details if st else []):
        bad = error_details_pb2.BadRequest()
        info = error_details_pb2.ErrorInfo()
        if d.Unpack(bad):
            fields.update({v.field: {"code": v.reason, "message": v.description} for v in bad.field_violations})
        elif d.Unpack(info):
            reason = info.reason
    return err.code(), fields, reason


class AccountClient:
    def __init__(self, addr=None, secret=None, channel=None, timeout=None):
        self.addr = addr or os.environ.get("ACCOUNT_SERVICE_ADDR", "accountservice:8081")
        self.secret = secret or os.environ.get("ACCOUNT_SERVICE_SECRET", "")
        self._channel = channel
        self.timeout = timeout or config.GRPC_TIMEOUT_S
        self._stub = None

    @property
    def stub(self):
        if self._stub is None:
            if self._channel is None:
                self._channel = grpc.insecure_channel(self.addr)
            self._stub = apbg.AccountServiceStub(self._channel)
        return self._stub

    def _call(self, method, request, token=None):
        md = [("x-service-name", SERVICE_NAME), ("x-service-secret", self.secret)]
        if token:
            md.append(("x-account-session", token))
        try:
            return getattr(self.stub, method)(request, metadata=md, timeout=self.timeout)
        except grpc.RpcError as e:
            code, fields, reason = _decode(e)
            if code == grpc.StatusCode.INVALID_ARGUMENT and fields:
                raise ValidationFailed(fields) from None
            if code in (grpc.StatusCode.UNAVAILABLE, grpc.StatusCode.DEADLINE_EXCEEDED):
                raise AccountUnavailable(reason or "unreachable") from None
            e.reason = reason
            raise

    # --- registration -------------------------------------------------------------------------
    @staticmethod
    def registration_input(f):
        return apb.RegistrationInput(
            first_name=f.get("first_name") or "", last_name=f.get("last_name") or "", email=f.get("email") or "", phone=f.get("phone") or "",
            address=apb.AccountAddress(line1=f.get("address_line1") or "", line2=f.get("address_line2") or "", city=f.get("city") or "",
                                       state=f.get("state") or "", zip=f.get("zip") or "", country=f.get("country") or ""),
            password=f.get("password") or "", confirm_password=f.get("confirm_password") or "", preferred_currency=f.get("preferred_currency") or "")

    def validate(self, fields, only=None):
        """-> (errors, normalized). errors: {field: {"code","message"}} (empty when valid), limited to
        `only` when given. normalized: server-normalized values (E.164 phone, upper-case state, ZIP+4)."""
        res = self._call("ValidateRegistration", self.registration_input(fields))
        out = {k: {"code": v.code, "message": v.message} for k, v in res.fields.items()}
        if only:
            out = {k: v for k, v in out.items() if k in only}
        n, a = res.normalized, res.normalized.address
        normalized = {"first_name": n.first_name, "last_name": n.last_name, "email": n.email, "phone": n.phone,
                      "line1": a.line1, "line2": a.line2, "city": a.city, "state": a.state, "zip": a.zip, "country": a.country}
        return out, normalized

    def begin_signup(self, session_id, fields):
        r = self._call("BeginAssistantSignup", apb.AssistantSignupRequest(input=self.registration_input(fields), session_id=session_id))
        return {"outcome": r.outcome, "customer_id": r.customer_id, "handoff": r.handoff}

    def activation(self, session_id):
        r = self._call("GetActivation", apb.GetActivationRequest(session_id=session_id))
        return {"pending": r.pending, "email_display": r.email_display}

    # --- session-derived customer data ---------------------------------------------------------
    def resolve(self, token):
        if not token:
            return None
        r = self._call("ResolveSession", dpb.Empty(), token)
        if not r.authenticated:
            return None
        return {"customer_id": r.customer_id, "first_name": r.first_name, "profile_complete": r.profile_complete,
                "preferred_currency": r.preferred_currency}

    def profile(self, token):
        p = self._call("GetProfile", dpb.Empty(), token)
        return {"customer_id": p.customer_id, "first_name": p.first_name, "last_name": p.last_name, "email": p.email_display,
                "phone_e164": p.phone_e164, "preferred_currency": p.preferred_currency, "profile_complete": p.profile_complete}

    def addresses(self, token):
        r = self._call("ListAddresses", dpb.Empty(), token)
        return [{"id": a.id, "label": a.label, "line1": a.line1, "line2": a.line2, "city": a.city, "state": a.state, "zip": a.zip,
                 "country": a.country, "is_default": a.is_default} for a in r.addresses]

    @staticmethod
    def _money(m):
        return f"{m.units + m.nanos / 1e9:.2f} {m.currency_code}"

    def orders(self, token, page_size=5):
        r = self._call("ListOrders", apb.ListOrdersRequest(page_size=max(1, min(int(page_size or 5), 10))), token)
        return [{"order_id": o.order_id, "placed_at": time.strftime("%Y-%m-%d", time.gmtime(o.placed_at)) if o.placed_at else "",
                 "status": o.status, "total": self._money(o.total), "item_count": o.item_count,
                 "items": list(o.item_names)} for o in r.orders], r.queue_depth

    def order(self, token, order_id):
        o = self._call("GetOrder", apb.OrderRef(order_id=order_id), token)
        return {"order_id": o.order_id, "placed_at": time.strftime("%Y-%m-%d", time.gmtime(o.placed_at)) if o.placed_at else "",
                "status": o.status, "channel": o.channel, "tracking_id": o.tracking_id,
                "subtotal": self._money(o.subtotal), "shipping": self._money(o.shipping), "total": self._money(o.total),
                "payment": f"{o.payment_brand} ending {o.payment_last4}" if o.payment_last4 else "",
                "items": [{"name": i.name, "quantity": i.quantity, "unit_price": self._money(i.unit_price)} for i in o.items]}

    def payment_methods(self, token):
        r = self._call("ListPaymentMethods", dpb.Empty(), token)
        return [{"id": m.id, "brand": m.brand, "last4": m.last4, "is_default": m.is_default} for m in r.methods]
