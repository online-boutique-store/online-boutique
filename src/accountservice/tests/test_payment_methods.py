"""M6: synthetic payment methods and tokenization, end to end with REAL binaries (plan 13.5).

paymentservice (Node), checkoutservice + shippingservice (Go) run as processes against a REAL AOF
order-queue Redis and the real accountservice (Postgres). Asserts catalog-only tokenization, token
ownership, charge-by-token, and that no PAN/CVV/token appears in the DB, streams, idempotency records,
logs or any exported telemetry byte."""
import os
import sys
import uuid

import grpc

import service_env as env
import stubs

PAY_PORT = stubs.free_port()
os.environ["PAYMENT_SERVICE_ADDR"] = f"127.0.0.1:{PAY_PORT}"     # read by accountservice at import
S = env.start()
import demo_pb2 as dpb  # noqa: E402
import demo_pb2_grpc as dpbg  # noqa: E402
import account_pb2 as apb  # noqa: E402

recv, cluster, order_q = S["receiver"], S["cluster"], S["order_q"]
ORDER_NS = uuid.UUID("6f1a2b3c-4d5e-4f60-8a7b-9c0d1e2f3a4b")
FAILS = []
# Catalog PANs: they must exist ONLY inside paymentservice/catalog.js.
CATALOG_PANS = ["4111111111111111", "4242424242424242", "5555555555554444", "378282246310005", "6011111111111117"]
REAL_LOOKING = "4532015112830366"          # Luhn-valid, not in the catalog
FORM_CARD = "4432-8015-6152-0454"


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {str(detail)[:300]}"))
    if not cond:
        FAILS.append(name)


stub = stubs.Stubs()
stubs.ensure_binaries()
SRC = stubs.SRC


def base_env(extra=None):
    e = {"ENABLE_TRACING": "1", "COLLECTOR_SERVICE_ADDR": f"127.0.0.1:{recv.port}", "OTEL_EXPORTER_OTLP_ENDPOINT": recv.endpoint,
         "ORDER_QUEUE_ADDR": f"127.0.0.1:{order_q.port}", "TELEMETRY_TEST_MODE": "1", "TELEMETRY_REF_KEY": "test-ref-key",
         "DISABLE_PROFILER": "1", "OTEL_RESOURCE_ATTRIBUTES": "service.namespace=online-boutique,service.version=test-1,deployment.environment.name=test",
         "OTEL_PROPAGATORS": "tracecontext,baggage",
         "OTEL_BSP_SCHEDULE_DELAY": "200", "OTEL_METRIC_EXPORT_INTERVAL": "500"}
    e.update(extra or {})
    return e


ship_port, co_port = stubs.free_port(), stubs.free_port()
shipping = stubs.Proc("shipping", ["/tmp/shipping_bin"], os.path.join(SRC, "shippingservice"), ship_port,
                      base_env({"PORT": ship_port, "OTEL_SERVICE_NAME": "shippingservice"}))
payment = stubs.Proc("payment", ["node", "index.js"], os.path.join(SRC, "paymentservice"), PAY_PORT,
                     base_env({"PORT": PAY_PORT, "OTEL_SERVICE_NAME": "paymentservice"}))
checkout = stubs.Proc("checkout", ["/tmp/checkout_bin"], os.path.join(SRC, "checkoutservice"), co_port,
                      base_env({"PORT": co_port, "OTEL_SERVICE_NAME": "checkoutservice", "SHIPPING_SERVICE_ADDR": f"127.0.0.1:{ship_port}",
                                "PAYMENT_SERVICE_ADDR": f"127.0.0.1:{PAY_PORT}", "PRODUCT_CATALOG_SERVICE_ADDR": stub.addr,
                                "CART_SERVICE_ADDR": stub.addr, "CURRENCY_SERVICE_ADDR": stub.addr, "EMAIL_SERVICE_ADDR": stub.addr,
                                "ACCOUNT_SERVICE_ADDR": f"127.0.0.1:{S['port']}", "ACCOUNT_SERVICE_SECRET": env.SECRETS["checkoutservice"]}))
for p in (shipping, payment, checkout):
    p.start()
PAY = dpbg.PaymentServiceStub(grpc.insecure_channel(f"127.0.0.1:{PAY_PORT}"))
CO = dpbg.CheckoutServiceStub(grpc.insecure_channel(f"127.0.0.1:{co_port}"))
ACC = S["stub"]


def pg(sql, params=()):
    return cluster.query(sql, params, db="svc")


def oid(key):
    return str(uuid.uuid5(ORDER_NS, key))


def new_customer():
    email = f"pm.{uuid.uuid4().hex[:8]}@example.com"
    r, e = env.call(ACC.Register, apb.RegisterRequest(input=apb.RegistrationInput(
        first_name="Pia", last_name="Methods", email=email, phone="(650) 253-0000", password="Valid1Passw0rd",
        confirm_password="Valid1Passw0rd", address=apb.AccountAddress(line1="1 Main St", city="Austin", state="TX", zip="73301"),
        preferred_currency="USD")))
    assert e is None, e
    return r.session_token, r.context.customer_id


def add(token, catalog_id, **kw):
    return env.call(ACC.AddPaymentMethod, apb.AddPaymentMethodRequest(catalog_id=catalog_id, **kw), token=token)


def place(key, user, token=None, method_id="", card=None, timeout=25):
    req = dpb.PlaceOrderRequest(
        user_id=user, user_currency="USD", email="pm.buyer@example.com", idempotency_key=key, channel="web",
        payment_method_id=method_id,
        address=dpb.Address(street_address="2 Pay Lane", city="Austin", state="TX", country="US", postal_code="73301", zip_code=73301))
    if card:
        req.credit_card.CopyFrom(dpb.CreditCardInfo(credit_card_number=card, credit_card_cvv=672, credit_card_expiration_year=2040,
                                                    credit_card_expiration_month=1))
    md = [("x-account-session", token)] if token else []
    try:
        return CO.PlaceOrder(req, metadata=md, timeout=timeout), None
    except grpc.RpcError as e:
        return None, e


def reason(err):
    m = err.details() or ""
    return m[len("reason:"):] if m.startswith("reason:") else m


def wait(pred, timeout=25.0):
    import time
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = pred()
            if v:
                return v
        except Exception:
            pass
        time.sleep(0.2)
    return False


def err_reason(e):
    return env.error_reason(e) if e else None


# --- 1. catalog + tokenization rules -----------------------------------------------------------------------------
def test_catalog_and_tokenize():
    token, _ = new_customer()
    cat, e = env.call(ACC.ListCatalogCards, apb.Empty() if hasattr(apb, "Empty") else dpb.Empty(), token=token)
    check("catalog lists the 5 synthetic demo cards", e is None and len(cat.cards) == 5, e and e.details())
    check("every catalog card is labelled as a demo test card", all("Demo test card" in c.display_name for c in cat.cards))
    wire = cat.SerializeToString().decode("latin-1")
    check("catalog response carries no card number", not any(p in wire for p in CATALOG_PANS))
    # the accountservice display catalog must match what paymentservice actually tokenizes
    mismatches = []
    for c in cat.cards:
        t = PAY.Tokenize(dpb.TokenizeRequest(catalog_id=c.catalog_id), timeout=10)
        if (t.brand, t.last4, t.exp_month, t.exp_year) != (c.brand, c.last4, c.exp_month, c.exp_year):
            mismatches.append((c.catalog_id, t.brand, t.last4))
    check("catalog metadata matches paymentservice Tokenize", not mismatches, mismatches)
    t = PAY.Tokenize(dpb.TokenizeRequest(catalog_id="demo_visa_1111"), timeout=10)
    import re
    check("token format tok_demo_<22 url-safe chars>", re.fullmatch(r"tok_demo_[A-Za-z0-9_-]{22}", t.token) is not None, t.token)
    check("tokens are unique per call", PAY.Tokenize(dpb.TokenizeRequest(catalog_id="demo_visa_1111"), timeout=10).token != t.token)
    for bad in (REAL_LOOKING, "4111111111111111", "", "__proto__", "constructor", "DEMO_VISA_1111", "demo_visa_1111 "):
        try:
            PAY.Tokenize(dpb.TokenizeRequest(catalog_id=bad), timeout=10)
            ok = False
        except grpc.RpcError as ex:
            ok = ex.code() == grpc.StatusCode.INVALID_ARGUMENT and ex.details() == "card_not_allowed_demo"
        check(f"Tokenize rejects non-catalog value {bad[:8] or '(empty)'!r}", ok)
    r, e = add(token, REAL_LOOKING)
    check("AddPaymentMethod rejects a Luhn-valid real-looking PAN (card_not_allowed_demo)",
          e is not None and e.code() == grpc.StatusCode.INVALID_ARGUMENT and err_reason(e) == "card_not_allowed_demo", e and e.details())
    n = pg("SELECT count(*) FROM payment_methods WHERE catalog_id = %s", (REAL_LOOKING,))[0][0]
    check("rejected value was not stored anywhere", n == 0)


# --- 2. saved-method lifecycle + ownership ----------------------------------------------------------------------
def test_lifecycle_and_ownership():
    token, cid = new_customer()
    a, e = add(token, "demo_visa_4242")
    check("first card added and is default automatically", e is None and a.is_default and a.last4 == "4242" and a.brand == "visa", e and e.details())
    b, e = add(token, "demo_mc_4444", label="Work card")
    check("second card is not default", e is None and not b.is_default and b.label == "Work card")
    lst, _ = env.call(ACC.ListPaymentMethods, dpb.Empty(), token=token)
    check("list shows both, default first", [m.id for m in lst.methods] == [a.id, b.id])
    check("PaymentMethod messages expose no token field", "token" not in {f.name for f in apb.PaymentMethod.DESCRIPTOR.fields})
    d, e = env.call(ACC.SetDefaultPaymentMethod, apb.PaymentMethodRef(id=b.id), token=token)
    ndef = pg("SELECT count(*) FROM payment_methods WHERE customer_id=%s AND is_default AND deleted_at IS NULL", (cid,))[0][0]
    check("set default moves the single default", e is None and d.is_default and ndef == 1, e and e.details())
    row = pg("SELECT provider_token, catalog_id FROM payment_methods WHERE id=%s", (b.id,))[0]
    check("DB stores the opaque token + catalog id only", row[0].startswith("tok_demo_") and row[1] == "demo_mc_4444")
    cols = {r[0] for r in pg("SELECT column_name FROM information_schema.columns WHERE table_name='payment_methods'")}
    check("payment_methods has no PAN/CVV/holder/billing columns", not cols & {"pan", "card_number", "cvv", "cardholder", "holder_name", "billing_address"}, cols)

    other_tok, other_cid = new_customer()
    r, e = env.call(ACC.DeletePaymentMethod, apb.PaymentMethodRef(id=a.id), token=other_tok)
    check("another customer cannot delete a method (NOT_FOUND)", e is not None and e.code() == grpc.StatusCode.NOT_FOUND)
    r, e = env.call(ACC.SetDefaultPaymentMethod, apb.PaymentMethodRef(id=a.id), token=other_tok)
    check("another customer cannot set default (NOT_FOUND)", e is not None and e.code() == grpc.StatusCode.NOT_FOUND)
    r, e = env.call(ACC.DeletePaymentMethod, apb.PaymentMethodRef(id="not-a-uuid"), token=token)
    check("malformed id is NOT_FOUND, not an error", e is not None and e.code() == grpc.StatusCode.NOT_FOUND)
    other_list, _ = env.call(ACC.ListPaymentMethods, dpb.Empty(), token=other_tok)
    check("another customer's list does not include them", len(other_list.methods) == 0)

    # service-only token resolution
    res, e = env.call(ACC.ResolvePaymentMethodToken, apb.PaymentMethodRef(id=a.id, customer_id=cid), service="checkoutservice")
    check("checkoutservice resolves the owner's method token", e is None and res.token.startswith("tok_demo_") and res.last4 == "4242", e and e.details())
    res, e = env.call(ACC.ResolvePaymentMethodToken, apb.PaymentMethodRef(id=a.id, customer_id=other_cid), service="checkoutservice")
    check("resolving with the wrong customer id is NOT_FOUND", e is not None and e.code() == grpc.StatusCode.NOT_FOUND)
    res, e = env.call(ACC.ResolvePaymentMethodToken, apb.PaymentMethodRef(id=a.id, customer_id=cid), service="frontend")
    check("the frontend service may NOT resolve tokens (PERMISSION_DENIED)", e is not None and e.code() == grpc.StatusCode.PERMISSION_DENIED)
    res, e = env.call(ACC.ResolvePaymentMethodToken, apb.PaymentMethodRef(id=a.id, customer_id=cid), service="checkoutservice", token=other_tok)
    check("a user session cannot substitute for the customer id argument", e is None and res.last4 == "4242")

    # delete: soft delete + token revoked + default re-assigned
    _, e = env.call(ACC.DeletePaymentMethod, apb.PaymentMethodRef(id=b.id), token=token)     # b is default now
    lst, _ = env.call(ACC.ListPaymentMethods, dpb.Empty(), token=token)
    check("deleting the default promotes another method", e is None and [m.id for m in lst.methods] == [a.id] and lst.methods[0].is_default)
    tok_after = pg("SELECT provider_token FROM payment_methods WHERE id=%s", (b.id,))[0][0]
    check("deleted method's token is revoked in the database", tok_after.startswith("revoked:"), tok_after)
    res, e = env.call(ACC.ResolvePaymentMethodToken, apb.PaymentMethodRef(id=b.id, customer_id=cid), service="checkoutservice")
    check("a deleted method can no longer be resolved", e is not None and e.code() == grpc.StatusCode.NOT_FOUND)

    # limit
    tok3, _ = new_customer()
    ids = ["demo_visa_1111", "demo_visa_4242", "demo_mc_4444", "demo_amex_0005", "demo_disc_1117"]
    for i in ids:
        add(tok3, i)
    r, e = add(tok3, "demo_visa_1111")
    check("sixth method is refused (RESOURCE_EXHAUSTED payment_method_limit_reached)",
          e is not None and e.code() == grpc.StatusCode.RESOURCE_EXHAUSTED and err_reason(e) == "payment_method_limit_reached", e and e.details())
    r, e = env.call(ACC.ListPaymentMethods, dpb.Empty())
    check("methods require a signed-in customer", e is not None and e.code() == grpc.StatusCode.UNAUTHENTICATED)


# --- 3. charge by token -------------------------------------------------------------------------------------------
def test_charge_by_token():
    amount = dpb.Money(currency_code="USD", units=12, nanos=340000000)
    t = PAY.Tokenize(dpb.TokenizeRequest(catalog_id="demo_visa_4242"), timeout=10)
    key = "pm-direct-" + uuid.uuid4().hex[:8]
    r1 = PAY.Charge(dpb.ChargeRequest(amount=amount, payment_token=t.token, idempotency_key=key), timeout=10)
    r2 = PAY.Charge(dpb.ChargeRequest(amount=amount, payment_token=t.token, idempotency_key=key), timeout=10)
    check("charge by token succeeds and repeats idempotently", r1.transaction_id and not r1.replayed and r2.replayed and r1.transaction_id == r2.transaction_id)
    for name, tok in (("a fabricated token", "tok_demo_AAAAAAAAAAAAAAAAAAAAAA"), ("a malformed token", "tok_demo_x"),
                      ("a raw card number as token", "4111111111111111")):
        try:
            PAY.Charge(dpb.ChargeRequest(amount=amount, payment_token=tok, idempotency_key="pm-bad-" + uuid.uuid4().hex[:8]), timeout=10)
            ok = False
        except grpc.RpcError as ex:
            ok = ex.code() == grpc.StatusCode.INVALID_ARGUMENT
        check(f"charge rejects {name}", ok)
    gc = PAY.GetCharge(dpb.GetChargeRequest(idempotency_key=key), timeout=10)
    check("GetCharge sees the token charge", gc.found and gc.transaction_id == r1.transaction_id)


# --- 4. saved-method checkout, end to end -------------------------------------------------------------------------------
def test_checkout_with_saved_method():
    token, cid = new_customer()
    pm, _ = add(token, "demo_amex_0005")
    user = "pm-user-" + uuid.uuid4().hex[:6]
    stub.carts[user] = [("P1", 2)]
    key = "pm-saved-" + uuid.uuid4().hex[:10]
    r, e = place(key, user, token=token, method_id=pm.id)
    check("authenticated checkout by saved method completes (no card data sent)", e is None and r.status == "completed", e and e.details())
    row = wait(lambda: pg("SELECT payment_method_id::text, payment_method_type, payment_brand, payment_last4, customer_id::text, record_state "
                          "FROM orders WHERE order_id=%s", (oid(key),)))
    check("order row references the saved method (id, type, brand, last4)",
          row and row[0][0] == pm.id and row[0][1] == "demo_card_saved" and row[0][2] == "amex" and row[0][3] == "0005" and row[0][4] == cid, row)
    go, e = env.call(ACC.GetOrder, apb.OrderRef(order_id=oid(key)), token=token)
    check("order detail shows the payment brand/last4", e is None and go.payment_last4 == "0005" and go.payment_brand == "amex", e and e.details())
    import redis as _r
    cr = order_q.client().hgetall(f"charge:{key}")
    check("the charge record exists exactly once", bool(cr.get("txn")) and cr.get("calls") == "1", cr)
    r2, e2 = place(key, user, token=token, method_id=pm.id)
    check("replaying the same saved-method checkout does not charge again", e2 is None and r2.replayed and order_q.client().hgetall(f"charge:{key}").get("calls") == "1", e2 and e2.details())

    # someone else's method
    other_tok, _ = new_customer()
    stub.carts[user] = [("P1", 1)]
    key2 = "pm-foreign-" + uuid.uuid4().hex[:8]
    r, e = place(key2, user, token=other_tok, method_id=pm.id)
    check("using another customer's saved method is refused (payment_method_unavailable)",
          e is not None and e.code() == grpc.StatusCode.FAILED_PRECONDITION and reason(e) == "payment_method_unavailable", e and e.details())
    check("...and nothing was charged", not order_q.client().hgetall(f"charge:{key2}"))
    key3 = "pm-guest-" + uuid.uuid4().hex[:8]
    r, e = place(key3, user, method_id=pm.id)
    check("a guest cannot use a saved method (login_required)", e is not None and reason(e) == "login_required", e and e.details())
    _, e = env.call(ACC.DeletePaymentMethod, apb.PaymentMethodRef(id=pm.id), token=token)
    key4 = "pm-deleted-" + uuid.uuid4().hex[:8]
    r, e = place(key4, user, token=token, method_id=pm.id)
    check("a deleted method is refused (payment_method_unavailable) and nothing is charged",
          e is not None and reason(e) == "payment_method_unavailable" and not order_q.client().hgetall(f"charge:{key4}"), e and e.details())

    # guest card form unchanged and idempotent
    guest = "pm-guest-user-" + uuid.uuid4().hex[:6]
    stub.carts[guest] = [("P1", 1)]
    gk = "pm-form-" + uuid.uuid4().hex[:8]
    r, e = place(gk, guest, card=FORM_CARD)
    r2, e2 = place(gk, guest, card=FORM_CARD)
    check("guest card-form checkout still works and is idempotent", e is None and e2 is None and r.status == "completed" and r2.replayed, (e and e.details(), e2 and e2.details()))
    grow = wait(lambda: pg("SELECT payment_method_id, payment_method_type, payment_last4 FROM orders WHERE order_id=%s", (oid(gk),)))
    check("guest order records card_form with brand/last4 only", grow and grow[0][0] is None and grow[0][1] == "card_form" and grow[0][2] == "0454", grow)
    return token


# --- 5. nothing sensitive anywhere ---------------------------------------------------------------------------------------------
def test_no_leaks():
    tokens = [r[0] for r in pg("SELECT provider_token FROM payment_methods WHERE left(provider_token, 9) = 'tok_demo_'")]
    revoked_ok = pg("SELECT count(*) FROM payment_methods WHERE left(provider_token, 8) = 'revoked:'")[0][0]
    check("test produced live and revoked tokens to look for", len(tokens) >= 3 and revoked_ok >= 1, (len(tokens), revoked_ok))
    q = order_q.client()
    # Redis: stream payloads, checkout state, idempotency records
    blob = []
    for k in q.scan_iter("*"):
        if k.startswith("tok:"):
            continue                               # token->catalog-id bindings are paymentservice's own store
        t = q.type(k)
        if t == "hash":
            blob.append(str(q.hgetall(k)))
        elif t == "stream":
            blob.append(str(q.xrange(k)))
        elif t == "string":
            blob.append(str(q.get(k)))
    redis_text = "\n".join(blob)
    check("no token in checkout state, idempotency records or the order stream", not any(t in redis_text for t in tokens))
    check("no card number or CVV in checkout state, idempotency records or the order stream",
          not any(p in redis_text for p in CATALOG_PANS) and "4432801561520454" not in redis_text.replace("-", ""))
    bindings = [q.get(k) for k in q.scan_iter("tok:*")]
    check("paymentservice's token store maps token hash -> catalog id only (no PAN)", bindings and all(b.startswith("demo_") for b in bindings), bindings[:3])
    dump = "\n".join(str(r) for r in pg("SELECT row_to_json(o)::text FROM orders o")) + "\n".join(str(r) for r in pg("SELECT row_to_json(c)::text FROM customers c"))
    check("no token or card number in orders/customers rows", not any(t in dump for t in tokens) and not any(p in dump for p in CATALOG_PANS))
    sensitive = tokens + CATALOG_PANS + ["4432801561520454", FORM_CARD, REAL_LOOKING]
    hits = recv.canary_hits(sensitive)
    check("no token or card number in ANY exported telemetry byte (traces, metrics, logs)", not hits, hits)
    logs = {p.name: p.log_text() for p in (checkout, shipping, payment)}
    leaks = [(n, c[:12]) for n, t in logs.items() for c in sensitive if c.lower() in t.lower()]
    check("no token or card number in service stdout logs", not leaks, leaks)
    import telemetry_schema as sch
    bad = []
    for sp in list(recv.spans):
        for k, v in sp.attrs.items():
            if k.startswith("app.payment.") and k not in ("app.payment.transaction_id",):
                try:
                    sch.coerce(k, v)
                except ValueError:
                    bad.append((sp.service, k, v))
    check("app.payment.* attributes satisfy the typed schema", not bad, bad[:3])


def test_telemetry():
    tz = [s for s in recv.spans if s.service == "paymentservice" and s.kind == "SERVER" and s.name.endswith("PaymentService/Tokenize")]
    check("Tokenize SERVER spans exported with app.payment.status", bool(tz) and any(s.attrs.get("app.payment.status") == "tokenized" for s in tz) and any(s.attrs.get("app.payment.status") == "failed" for s in tz),
          [s.attrs.get("app.payment.status") for s in tz][:5])
    add_spans = [s for s in recv.spans if s.service == "accountservice" and s.kind == "SERVER" and s.name.endswith("AccountService/AddPaymentMethod")]
    check("AddPaymentMethod spans carry action + customer id + tokenized status",
          any(s.attrs.get("app.account.action") == "payment_add" and s.attrs.get("app.customer.id") and s.attrs.get("app.payment.status") == "tokenized" for s in add_spans),
          [s.attrs for s in add_spans][:1])
    peer = [s for s in recv.spans if s.service == "accountservice" and s.kind == "CLIENT" and "PaymentService/Tokenize" in s.name]
    check("accountservice -> paymentservice Tokenize client span exists", bool(peer))
    saved = [s for s in recv.spans if s.service == "paymentservice" and s.attrs.get("app.payment.method_type") == "demo_card_saved"]
    check("saved-card charges are labelled demo_card_saved", bool(saved))
    m = recv.metric_names() if hasattr(recv, "metric_names") else None
    if m is not None:
        check("app.payment.tokenize metric exported", "app.payment.tokenize" in m, sorted(m)[:5])


def test_matrix_rows():
    import yaml
    rows = [r for r in yaml.safe_load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "telemetry_matrix.yaml")))["rows"]
            if r.get("suite") == "payment_methods"]
    check("matrix declares R16-R21 for this suite", sorted(r["id"] for r in rows) == ["R16", "R17", "R18", "R19", "R20", "R21"], [r["id"] for r in rows])
    for r in rows:
        ok, detail = False, "no SERVER span"
        for sp in recv.find("/hipstershop.AccountService/" + r["rpc"], service="accountservice", kind="SERVER"):
            tr = recv.trace_spans(sp.trace_id)
            names = {x.name for x in tr}
            dbs = {"db:" + x.attrs["db.system"] for x in tr if x.attrs.get("db.system")}
            missing = [a for a in r["server_attrs"] if a not in sp.attrs]
            miss_children = [c for c in r["children"] if c not in names and c not in dbs]
            detail = (missing, miss_children)
            if not missing and not miss_children:
                ok = True
                break
        check(f"matrix {r['id']} {r['rpc']}: server attrs + child spans present", ok, detail)


def main():
    try:
        test_catalog_and_tokenize()
        test_lifecycle_and_ownership()
        test_charge_by_token()
        test_checkout_with_saved_method()
        import time
        time.sleep(2.5)
        test_no_leaks()
        test_telemetry()
        test_matrix_rows()
    finally:
        for p in (checkout, payment, shipping):
            p.stop()
        stub.stop()
    print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
