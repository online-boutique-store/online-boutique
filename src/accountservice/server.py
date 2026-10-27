"""AccountService gRPC server.

Every RPC requires a valid internal service credential (x-service-name / x-service-secret); user
RPCs additionally require the opaque x-account-session token, from which the customer is derived
(callers can never assert a customer id). Handlers raise Fail for client-visible errors; the
wrapper maps Fail / AccountStoreUnavailable / unexpected errors onto gRPC status, records only the
exception TYPE on spans, and emits metrics + typed business attributes for every call.
"""
import functools
import hmac
import time
import uuid
from concurrent import futures

import grpc
from google.protobuf import any_pb2
from google.rpc import code_pb2, error_details_pb2, status_pb2
from grpc_status import rpc_status
from opentelemetry import trace

import account_pb2 as pb
import demo_pb2
import demo_pb2_grpc
import account_pb2_grpc as pb_grpc
import accounts
import accounts_db
import config
import telemetry as tel
import telemetry_sanitize as san
import validation

AccountStoreUnavailable = accounts_db.AccountStoreUnavailable


class Fail(Exception):
    def __init__(self, code, message, fields=None, reason=None):
        super().__init__(message)
        self.code, self.message, self.fields, self.reason = code, message, fields or {}, reason


def _status(code, message, fields=None, reason=None):
    details = []
    if fields:
        bad = error_details_pb2.BadRequest()
        for name, e in fields.items():
            bad.field_violations.add(field=name, reason=e["code"], description=e["message"])
        a = any_pb2.Any()
        a.Pack(bad)
        details.append(a)
    if reason:
        info = error_details_pb2.ErrorInfo(reason=reason, domain="accountservice")
        a = any_pb2.Any()
        a.Pack(info)
        details.append(a)
    return rpc_status.to_status(status_pb2.Status(code=code, message=message, details=details))


def validation_fail(errors):
    return Fail(code_pb2.INVALID_ARGUMENT, "validation_failed", fields=errors)


def _record_validation(errors):
    span = trace.get_current_span()
    tel.set_attrs(span, {"app.validation.fields": sorted(errors), "app.validation.codes": sorted({e["code"] for e in errors.values()})})
    for field, e in errors.items():
        tel.account_validation_failures.add(1, {"field": field, "code": e["code"]})


def rpc(action, auth="session"):
    """auth: session (required) | optional | public. All variants require the service credential."""
    def deco(fn):
        @functools.wraps(fn)
        def wrapper(self, request, context):
            t0, outcome = time.monotonic(), "ok"
            span = trace.get_current_span()
            try:
                caller = self._check_service(context)
                tel.set_attrs(span, {"app.account.action": action, "app.account.service_caller": caller})
                ident = None
                if auth in ("session", "optional"):
                    ident = self._identity(context)
                    if ident is None and auth == "session":
                        raise Fail(code_pb2.UNAUTHENTICATED, "login_required")
                if ident:
                    tel.set_attrs(span, {"app.customer.id": ident["customer_id"], "app.customer.authenticated": True,
                                         "app.customer.type": "authenticated",
                                         "app.account.profile_version": ident.get("profile_version", 1),
                                         "app.customer.profile_complete": ident.get("profile_complete", False)})
                else:
                    tel.set_attrs(span, {"app.customer.authenticated": False, "app.customer.type": "anonymous"})
                tel.stamp_app_attrs(span)
                return fn(self, request, context, ident)
            except Fail as f:
                outcome = f.reason or ("validation_failed" if f.fields else "denied")
                if f.fields:
                    _record_validation(f.fields)
                span.set_attribute("error.type", "Fail")
                context.abort_with_status(_status(f.code, f.message, f.fields, f.reason))
            except AccountStoreUnavailable as e:
                outcome = e.reason
                span.set_attribute("error.type", "AccountStoreUnavailable")
                context.abort_with_status(_status(code_pb2.UNAVAILABLE, "account_store_unavailable", reason=e.reason))
            except Exception as e:  # noqa: BLE001 - never leak details; type only
                outcome = "error"
                tel.record_exception(span, e)
                tel.warn("account.rpc.error", **{"error.type": type(e).__name__, "app.account.action": action})
                context.abort_with_status(_status(code_pb2.INTERNAL, "internal_error"))
            finally:
                tel.account_actions.add(1, {"action": action, "outcome": outcome})
                tel.rpc_duration.record(time.monotonic() - t0, {"action": action, "outcome": outcome})
        return wrapper
    return deco


def _md(context):
    return {k: v for k, v in context.invocation_metadata()}


# Display metadata for the synthetic demo catalog (no card numbers; those live only in paymentservice).
# tests/test_payment_methods.py checks this matches what paymentservice.Tokenize returns.
DEMO_CATALOG = (
    {"catalog_id": "demo_visa_1111", "brand": "visa", "last4": "1111", "exp_month": 12, "exp_year": 2034, "display_name": "Demo test card - Visa ending 1111"},
    {"catalog_id": "demo_visa_4242", "brand": "visa", "last4": "4242", "exp_month": 12, "exp_year": 2034, "display_name": "Demo test card - Visa ending 4242"},
    {"catalog_id": "demo_mc_4444", "brand": "mastercard", "last4": "4444", "exp_month": 12, "exp_year": 2034, "display_name": "Demo test card - Mastercard ending 4444"},
    {"catalog_id": "demo_amex_0005", "brand": "amex", "last4": "0005", "exp_month": 12, "exp_year": 2034, "display_name": "Demo test card - Amex ending 0005"},
    {"catalog_id": "demo_disc_1117", "brand": "discover", "last4": "1117", "exp_month": 12, "exp_year": 2034, "display_name": "Demo test card - Discover ending 1117"},
)
DEMO_CATALOG_IDS = frozenset(c["catalog_id"] for c in DEMO_CATALOG)


def _is_uuid(v):
    try:
        uuid.UUID(str(v))
        return True
    except ValueError:
        return False


class AccountServicer(pb_grpc.AccountServiceServicer):
    def __init__(self, store, consumer=None, payment=None):
        self.store = store
        self.consumer = consumer
        self.payment = payment or demo_pb2_grpc.PaymentServiceStub(grpc.insecure_channel(config.PAYMENT_SERVICE_ADDR))

    # --- auth plumbing ---------------------------------------------------------------------
    def _check_service(self, context):
        md = _md(context)
        name, secret = md.get("x-service-name", ""), md.get("x-service-secret", "")
        expected = config.service_secrets().get(name)
        if not expected or not hmac.compare_digest(expected.encode(), str(secret).encode()):
            raise Fail(code_pb2.UNAUTHENTICATED, "service_auth_failed", reason="service_auth_failed")
        return name

    def _token(self, context):
        return _md(context).get("x-account-session", "")

    def _identity(self, context):
        token = self._token(context)
        return self.store.session_account(token) if token else None

    # --- helpers ---------------------------------------------------------------------------
    @staticmethod
    def _input_dict(inp):
        a = inp.address
        return {"first_name": inp.first_name, "last_name": inp.last_name, "email": inp.email, "phone": inp.phone,
                "address_line1": a.line1, "address_line2": a.line2, "city": a.city, "state": a.state, "zip": a.zip,
                "country": a.country, "password": inp.password, "confirm_password": inp.confirm_password or None,
                "preferred_currency": inp.preferred_currency}

    @staticmethod
    def _register_pii(body):
        """Register this request's PII with the sanitizer before anything can reach telemetry."""
        san.register_person(body.get("first_name"), body.get("last_name"), body.get("email"))
        san.register_sensitive(body.get("phone"), body.get("address_line1"), body.get("address_line2"), body.get("city"))
        z = str(body.get("zip") or "")
        if len(z) >= 5:
            san.register_sensitive(z)

    @staticmethod
    def _context_msg(acct, token_ident=None):
        missing = validation.missing_profile_fields(acct)
        complete = acct.get("profile_version", 1) == 2 and not missing
        return pb.SessionContext(authenticated=True, customer_id=acct["id"], first_name=acct.get("first_name") or (acct.get("name") or "").split(" ")[0],
                                 profile_complete=complete, profile_version=acct.get("profile_version", 1),
                                 missing_fields=missing, preferred_currency=acct.get("preferred_currency", "USD"))

    @staticmethod
    def _profile_msg(acct):
        missing = validation.missing_profile_fields(acct)
        return pb.Profile(customer_id=acct["id"], first_name=acct.get("first_name") or "", last_name=acct.get("last_name") or "",
                          email_display=acct.get("email_display") or acct["email"], phone_display=validation.format_phone(acct.get("phone_e164")),
                          phone_e164=acct.get("phone_e164") or "", preferred_currency=acct.get("preferred_currency", "USD"),
                          profile_version=acct.get("profile_version", 1), profile_complete=acct.get("profile_version", 1) == 2 and not missing,
                          missing_fields=missing, created_via=acct.get("created_via", ""), created_at=acct.get("created_at") or 0)

    @staticmethod
    def _addr_msg(r):
        return pb.SavedAddress(id=r["id"], label=r.get("label") or "", line1=r["line1"], line2=r.get("line2") or "", city=r["city"],
                               state=r["state"], zip=r["zip"], country=r["country"], is_po_box=r["is_po_box"], is_default=r["is_default"],
                               address_verification_status=r["address_verification_status"])

    @staticmethod
    def _addr_fields(a):
        return {"address_line1": a.line1, "address_line2": a.line2, "city": a.city, "state": a.state, "zip": a.zip, "country": a.country}

    # --- identity --------------------------------------------------------------------------
    @rpc("validate", auth="public")
    def ValidateRegistration(self, request, context, ident):
        body = self._input_dict(request)
        self._register_pii(body)
        profile, _, errors = validation.validate_registration(body, require_password=bool(request.password))
        if errors:
            _record_validation(errors)
        a = profile["address"]
        normalized = pb.RegistrationInput(
            first_name=profile["first_name"], last_name=profile["last_name"], email=profile["email_display"], phone=profile["phone_e164"],
            address=pb.AccountAddress(line1=a["line1"] or "", line2=a["line2"] or "", city=a["city"], state=a["state"], zip=a["zip"], country=a["country"]),
            preferred_currency=profile["preferred_currency"])
        return pb.ValidationResult(fields={k: pb.FieldError(code=v["code"], message=v["message"]) for k, v in errors.items()}, normalized=normalized)

    @rpc("register", auth="public")
    def Register(self, request, context, ident):
        body = self._input_dict(request.input)
        self._register_pii(body)
        profile, password, errors = validation.validate_registration(body)
        if errors:
            raise validation_fail(errors)
        created, cid = self.store.register(profile, password)
        if not created:
            raise Fail(code_pb2.ALREADY_EXISTS, "email_taken", fields={"email": validation.err("email", "email_taken")}, reason="email_taken")
        tel.set_attrs(trace.get_current_span(), {"app.customer.id": cid, "app.account.created_via": "web", "app.account.profile_version": 2,
                                                "app.customer.authenticated": True, "app.customer.type": "authenticated"})
        acct = self.store.repo.get_customer(cid)
        token = self.store.create_session(acct)
        return pb.SessionResponse(session_token=token, context=self._context_msg(acct))

    @rpc("assistant_signup", auth="public")
    def BeginAssistantSignup(self, request, context, ident):
        body = self._input_dict(request.input)
        self._register_pii(body)
        profile, _, errors = validation.validate_registration(body, require_password=False)
        if errors:
            raise validation_fail(errors)
        if not request.session_id:
            raise Fail(code_pb2.INVALID_ARGUMENT, "session_required")
        out = self.store.create_pending(request.session_id, profile)
        tel.set_attrs(trace.get_current_span(), {"app.customer.id": out["customer_id"], "app.account.created_via": "assistant",
                                                "app.account.activation.outcome": out["outcome"], "app.account.profile_version": 2})
        return pb.SignupHandoff(outcome=out["outcome"], customer_id=out["customer_id"] or "", handoff=out["handoff"])

    @rpc("activate", auth="public")
    def GetActivation(self, request, context, ident):
        info = self.store.activation_status(request.session_id) if request.session_id else {"pending": False}
        return pb.ActivationInfo(pending=info["pending"], email_display=info.get("email_display", ""))

    @rpc("activate", auth="public")
    def Claim(self, request, context, ident):
        info = self.store.activation_status(request.session_id) if request.session_id else {"pending": False}
        if not info["pending"]:
            raise Fail(code_pb2.NOT_FOUND, "activation_not_found", reason="activation_not_found")
        perr = validation.password_error(request.password, str(info.get("email_display", "")).lower())
        if perr:
            raise validation_fail({"password": perr})
        acct, token = self.store.claim(request.session_id, request.password)
        if not acct:
            raise Fail(code_pb2.NOT_FOUND, "activation_not_found", reason="activation_not_found")
        tel.set_attrs(trace.get_current_span(), {"app.customer.id": acct["id"], "app.account.activation.outcome": "consumed",
                                                "app.customer.authenticated": True, "app.customer.type": "authenticated"})
        return pb.SessionResponse(session_token=token or "", context=self._context_msg(acct))

    @rpc("login", auth="public")
    def Login(self, request, context, ident):
        display, key, err = validation.validate_email(request.email)
        if err:
            tel.account_login.add(1, {"outcome": "invalid"})
            raise Fail(code_pb2.UNAUTHENTICATED, "invalid_credentials", reason="invalid_credentials")
        san.register_sensitive(request.email, request.password, min_len=4)
        if self.store.login_locked(key):
            tel.account_login.add(1, {"outcome": "locked"})
            raise Fail(code_pb2.RESOURCE_EXHAUSTED, "login_locked", reason="login_locked")
        acct = self.store.authenticate(key, request.password)
        if not acct:
            self.store.record_login_failure(key)
            self.store.record_login_event(None, "failed")
            tel.account_login.add(1, {"outcome": "failed"})
            raise Fail(code_pb2.UNAUTHENTICATED, "invalid_credentials", reason="invalid_credentials")
        self.store.clear_login_failures(key)
        self.store.record_login_event(acct["id"], "success")
        token = self.store.create_session(acct)
        tel.account_login.add(1, {"outcome": "success"})
        tel.set_attrs(trace.get_current_span(), {"app.customer.id": acct["id"], "app.customer.authenticated": True, "app.customer.type": "authenticated",
                                                "app.account.profile_version": acct.get("profile_version", 1)})
        return pb.SessionResponse(session_token=token, context=self._context_msg(acct))

    @rpc("logout", auth="optional")
    def Logout(self, request, context, ident):
        self.store.delete_session(self._token(context))
        return demo_pb2.Empty()

    @rpc("session_resolve", auth="optional")
    def ResolveSession(self, request, context, ident):
        if not ident:
            return pb.SessionContext(authenticated=False)
        missing = [] if ident.get("profile_complete") else ["profile"]
        return pb.SessionContext(authenticated=True, customer_id=ident["customer_id"], first_name=ident.get("first_name", ""),
                                 profile_complete=bool(ident.get("profile_complete")), profile_version=ident.get("profile_version", 1),
                                 missing_fields=missing if not ident.get("profile_complete") else [],
                                 preferred_currency=ident.get("preferred_currency", "USD"))

    # --- profile ---------------------------------------------------------------------------
    @rpc("profile_get")
    def GetProfile(self, request, context, ident):
        acct = self.store.repo.get_customer(ident["customer_id"])
        if not acct:
            raise Fail(code_pb2.NOT_FOUND, "not_found")
        return self._profile_msg(acct)

    @rpc("profile_update")
    def UpdateProfile(self, request, context, ident):
        body = {}
        for f in request.fields:
            if f in ("first_name", "last_name", "phone", "preferred_currency"):
                body[f] = getattr(request, f)
        san.register_person(body.get("first_name"), body.get("last_name"))
        san.register_sensitive(body.get("phone"))
        changes, errors = validation.validate_profile_update(body)
        if errors:
            raise validation_fail(errors)
        acct = self.store.repo.update_profile(ident["customer_id"], changes)
        if not acct:
            raise Fail(code_pb2.NOT_FOUND, "not_found")
        self.store.refresh_session(self._token(context), acct)
        tel.set_attrs(trace.get_current_span(), {"app.account.profile_version": acct["profile_version"]})
        return self._profile_msg(acct)

    # --- addresses -------------------------------------------------------------------------
    @rpc("address_list")
    def ListAddresses(self, request, context, ident):
        rows = self.store.repo.list_addresses(ident["customer_id"])
        tel.set_attrs(trace.get_current_span(), {"app.account.address_count": len(rows)})
        return pb.AddressList(addresses=[self._addr_msg(r) for r in rows])

    def _validated_address(self, a):
        fields = self._addr_fields(a)
        san.register_address({"line1": fields["address_line1"], "line2": fields["address_line2"], "city": fields["city"], "zip": fields["zip"]})
        address, errors = validation.validate_address(fields)
        if errors:
            raise validation_fail(errors)
        tel.set_attrs(trace.get_current_span(), {"app.shipping.is_po_box": address["is_po_box"]})
        return address

    def _refresh(self, context, customer_id):
        acct = self.store.repo.get_customer(customer_id)
        if acct:
            self.store.refresh_session(self._token(context), acct)

    @rpc("address_create")
    def CreateAddress(self, request, context, ident):
        address = self._validated_address(request.address)
        outcome, row = self.store.repo.create_address(ident["customer_id"], address, label=request.label or None, make_default=request.make_default)
        if outcome == "limit_reached":
            raise Fail(code_pb2.RESOURCE_EXHAUSTED, "address_limit_reached", reason="address_limit_reached")
        self._refresh(context, ident["customer_id"])
        return self._addr_msg(row)

    @rpc("address_update")
    def UpdateAddress(self, request, context, ident):
        address = self._validated_address(request.address)
        row = self.store.repo.update_address(ident["customer_id"], request.id, address, label=request.label or None)
        if not row:
            raise Fail(code_pb2.NOT_FOUND, "not_found")
        return self._addr_msg(row)

    @rpc("address_delete")
    def DeleteAddress(self, request, context, ident):
        if not self.store.repo.delete_address(ident["customer_id"], request.id):
            raise Fail(code_pb2.NOT_FOUND, "not_found")
        self._refresh(context, ident["customer_id"])
        return demo_pb2.Empty()

    @rpc("address_default")
    def SetDefaultAddress(self, request, context, ident):
        row = self.store.repo.set_default_address(ident["customer_id"], request.id)
        if not row:
            raise Fail(code_pb2.NOT_FOUND, "not_found")
        return self._addr_msg(row)

    # --- payment methods (synthetic demo catalog only) ----------------------------------------
    @staticmethod
    def _pm_msg(r):
        return pb.PaymentMethod(id=r["id"], brand=r["brand"], last4=r["last4"], exp_month=r["exp_month"],
                                exp_year=r["exp_year"], label=r.get("label") or "", is_default=r["is_default"])

    @rpc("payment_list", auth="optional")
    def ListCatalogCards(self, request, context, ident):
        return pb.CatalogCardList(cards=[pb.CatalogCard(**c) for c in DEMO_CATALOG])

    @rpc("payment_list")
    def ListPaymentMethods(self, request, context, ident):
        rows = self.store.repo.list_payment_methods(ident["customer_id"])
        tel.set_attrs(trace.get_current_span(), {"app.account.payment_method_count": len(rows)})
        return pb.PaymentMethodList(methods=[self._pm_msg(r) for r in rows])

    def _tokenize(self, catalog_id):
        """paymentservice exchanges a catalog id for a token; only catalog ids are ever sent."""
        span = trace.get_current_span()
        try:
            resp = self.payment.Tokenize(demo_pb2.TokenizeRequest(catalog_id=catalog_id), timeout=3)
        except grpc.RpcError as e:
            if e.code() == grpc.StatusCode.INVALID_ARGUMENT:
                tel.payment_tokenize.add(1, {"outcome": "rejected"})
                tel.set_attrs(span, {"app.payment.status": "failed"})
                raise Fail(code_pb2.INVALID_ARGUMENT, "card_not_allowed_demo", reason="card_not_allowed_demo")
            tel.payment_tokenize.add(1, {"outcome": "unavailable"})
            raise Fail(code_pb2.UNAVAILABLE, "payment_service_unavailable", reason="payment_service_unavailable")
        san.register_sensitive(resp.token)
        tel.payment_tokenize.add(1, {"outcome": "tokenized"})
        tel.set_attrs(span, {"app.payment.status": "tokenized", "app.payment.method_type": "demo_card_saved"})
        return resp

    @rpc("payment_add")
    def AddPaymentMethod(self, request, context, ident):
        catalog_id = request.catalog_id
        if catalog_id not in DEMO_CATALOG_IDS:      # never forward anything that is not a catalog id
            tel.payment_tokenize.add(1, {"outcome": "rejected"})
            raise Fail(code_pb2.INVALID_ARGUMENT, "card_not_allowed_demo", reason="card_not_allowed_demo")
        label = request.label.strip()[:40] or None
        tok = self._tokenize(catalog_id)
        card = {"catalog_id": catalog_id, "brand": tok.brand, "last4": tok.last4, "exp_month": tok.exp_month, "exp_year": tok.exp_year}
        outcome, row = self.store.repo.add_payment_method(ident["customer_id"], tok.token, card, label=label,
                                                          make_default=request.make_default)
        if outcome == "limit_reached":
            raise Fail(code_pb2.RESOURCE_EXHAUSTED, "payment_method_limit_reached", reason="payment_method_limit_reached")
        return self._pm_msg(row)

    @rpc("payment_delete")
    def DeletePaymentMethod(self, request, context, ident):
        if not _is_uuid(request.id) or not self.store.repo.delete_payment_method(ident["customer_id"], request.id):
            raise Fail(code_pb2.NOT_FOUND, "not_found")
        return demo_pb2.Empty()

    @rpc("payment_default")
    def SetDefaultPaymentMethod(self, request, context, ident):
        row = self.store.repo.set_default_payment_method(ident["customer_id"], request.id) if _is_uuid(request.id) else None
        if not row:
            raise Fail(code_pb2.NOT_FOUND, "not_found")
        return self._pm_msg(row)

    @rpc("payment_resolve", auth="public")
    def ResolvePaymentMethodToken(self, request, context, ident):
        # Service-only and customer-scoped: the caller (checkout) passes the customer id it resolved from
        # the shopper's session; a method belonging to anyone else is NOT_FOUND.
        if _md(context).get("x-service-name") not in config.TOKEN_RESOLVERS:
            raise Fail(code_pb2.PERMISSION_DENIED, "service_not_allowed", reason="service_not_allowed")
        if not (_is_uuid(request.id) and _is_uuid(request.customer_id)):
            raise Fail(code_pb2.NOT_FOUND, "not_found")
        tel.set_attrs(trace.get_current_span(), {"app.customer.id": request.customer_id})
        row = self.store.repo.resolve_payment_token(request.customer_id, request.id)
        if not row:
            raise Fail(code_pb2.NOT_FOUND, "not_found")
        san.register_sensitive(row["provider_token"])
        return pb.PaymentToken(token=row["provider_token"], brand=row["brand"], last4=row["last4"])

    @rpc("orders_list")
    def ListOrders(self, request, context, ident):
        import orders_db
        rows, nxt = orders_db.list_orders(self.store.repo, ident["customer_id"], request.page_size, request.cursor)
        depth = self.consumer.status()[0] if self.consumer else 0
        tel.set_attrs(trace.get_current_span(), {"app.order.item_count": len(rows)})
        out = [pb.OrderSummary(order_id=r["order_id"], placed_at=(r["placed_ms"] or 0) // 1000, status=r["status"],
                               total=demo_pb2.Money(currency_code=r["currency"], units=r["total_units"], nanos=r["total_nanos"]),
                               item_count=r["item_count"], item_names=list(r["item_names"] or [])) for r in rows]
        return pb.ListOrdersResponse(orders=out, next_cursor=nxt, queue_depth=depth)

    @rpc("order_view")
    def GetOrder(self, request, context, ident):
        import orders_db
        row, items = orders_db.get_order(self.store.repo, ident["customer_id"], request.order_id)
        if not row:
            raise Fail(code_pb2.NOT_FOUND, "not_found")
        tel.set_attrs(trace.get_current_span(), {"app.order.id": row["order_id"], "app.order.channel": row["channel"]})
        cur = row["currency"]
        money = lambda u, n: demo_pb2.Money(currency_code=cur, units=u or 0, nanos=n or 0)   # noqa: E731
        return pb.AccountOrder(
            order_id=row["order_id"], placed_at=(row["placed_ms"] or 0) // 1000, status=row["status"], currency=cur,
            subtotal=money(row["subtotal_units"], row["subtotal_nanos"]), shipping=money(row["shipping_units"], row["shipping_nanos"]),
            total=money(row["total_units"], row["total_nanos"]), tracking_id=row["tracking_id"] or "", channel=row["channel"],
            payment_brand=row["payment_brand"] or "", payment_last4=row["payment_last4"] or "", item_count=sum(i["quantity"] for i in items),
            items=[pb.AccountOrderItem(product_id=i["product_id"], name=i["name"], quantity=i["quantity"],
                                       unit_price=demo_pb2.Money(currency_code=i["currency"] or cur, units=i["unit_price_units"] or 0, nanos=i["unit_price_nanos"] or 0)) for i in items],
            ship_to=pb.SavedAddress(line1=row["ship_line1"] or "", line2=row["ship_line2"] or "", city=row["ship_city"] or "", state=row["ship_state"] or "",
                                    zip=row["ship_zip"] or "", country=row["ship_country"] or "US", is_po_box=bool(row["ship_is_po_box"])))

    @rpc("claim_guest_orders")
    def ClaimGuestOrders(self, request, context, ident):
        import orders_db
        n = orders_db.claim_guest_orders(self.store.repo, ident["customer_id"], tel.session_ref(request.session_id))
        return pb.ClaimResult(claimed=n)

    @rpc("orders_list")
    def GetOrderQueueStatus(self, request, context, ident):
        depth, oldest, dead = self.consumer.status() if self.consumer else (0, 0, 0)
        return pb.QueueStatus(depth=depth, oldest_age_seconds=oldest, dead=dead)


def build_server(store, address=None, max_workers=16, consumer=None, payment=None):
    from grpc_health.v1 import health, health_pb2, health_pb2_grpc

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))
    pb_grpc.add_AccountServiceServicer_to_server(AccountServicer(store, consumer, payment), server)
    hs = health.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(hs, server)
    hs.set("", health_pb2.HealthCheckResponse.SERVING)
    port = server.add_insecure_port(address or f"[::]:{config.PORT}")
    return server, port, hs


def main():
    tel.init()
    store = accounts.AccountStore()
    store.start()
    consumer = None
    if config.ORDER_QUEUE_ADDR:
        import orders
        consumer = orders.OrderConsumer(store.repo)
        consumer.start()
    server, port, _ = build_server(store, consumer=consumer)
    server.start()
    tel.event("accountservice.started")
    server.wait_for_termination()


if __name__ == "__main__":
    main()
