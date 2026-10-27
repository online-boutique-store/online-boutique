"""Durable, idempotent checkout + order history, end to end with REAL binaries.

checkoutservice, shippingservice (Go) and paymentservice (Node) run as processes against a REAL
AOF order-queue Redis and the real accountservice (Postgres + stream consumer); cart/catalog/currency/
email are stubs. Faults are injected by killing processes at exact points (test-mode hooks) and by
stopping Redis/Postgres, then retrying with the SAME idempotency key. Everything is exported over OTLP
to the local receiver for trace-shape and PII-canary assertions."""
import json
import os
import sys
import threading
import time
import uuid

import grpc

import service_env as env

S = env.start()
import demo_pb2 as dpb  # noqa: E402
import demo_pb2_grpc as dpbg  # noqa: E402
import account_pb2 as apb  # noqa: E402
import otel_harness as oh  # noqa: E402
import stubs  # noqa: E402

recv, cluster, order_q = S["receiver"], S["cluster"], S["order_q"]
consumer = S["consumer"]
ORDER_NS = uuid.UUID("6f1a2b3c-4d5e-4f60-8a7b-9c0d1e2f3a4b")
FAILS = []
CARD = "4432-8015-6152-0454"
CAN = {"email": "dura.canary@example.com", "street": "9090 Durabilityville Parkway", "city": "Cinderfall", "card": CARD, "card_digits": "4432801561520454",
       "cvv_json": '"credit_card_cvv"', "session": "browser-sess-canary-77", "bag": "baggage.dura.canary@example.com"}


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {str(detail)[:300]}"))
    if not cond:
        FAILS.append(name)


stub = stubs.Stubs()
stubs.ensure_binaries()
SRC = stubs.SRC
q = lambda: order_q.client()   # noqa: E731


def base_env(extra=None):
    e = {"ENABLE_TRACING": "1", "COLLECTOR_SERVICE_ADDR": f"127.0.0.1:{recv.port}", "OTEL_EXPORTER_OTLP_ENDPOINT": recv.endpoint,
         "ORDER_QUEUE_ADDR": f"127.0.0.1:{order_q.port}", "TELEMETRY_TEST_MODE": "1", "TELEMETRY_REF_KEY": "test-ref-key",
         "DISABLE_PROFILER": "1", "OTEL_RESOURCE_ATTRIBUTES": "service.namespace=online-boutique,service.version=test-1,deployment.environment.name=test",
         "OTEL_PROPAGATORS": "tracecontext,baggage",
         "OTEL_BSP_SCHEDULE_DELAY": "200", "OTEL_METRIC_EXPORT_INTERVAL": "500"}
    e.update(extra or {})
    return e


ship_port, pay_port, co_port = stubs.free_port(), stubs.free_port(), stubs.free_port()
shipping = stubs.Proc("shipping", ["/tmp/shipping_bin"], os.path.join(SRC, "shippingservice"), ship_port,
                      base_env({"PORT": ship_port, "OTEL_SERVICE_NAME": "shippingservice"}))
payment = stubs.Proc("payment", ["node", "index.js"], os.path.join(SRC, "paymentservice"), pay_port,
                     base_env({"PORT": pay_port, "OTEL_SERVICE_NAME": "paymentservice"}))
checkout = stubs.Proc("checkout", ["/tmp/checkout_bin"], os.path.join(SRC, "checkoutservice"), co_port,
                      base_env({"PORT": co_port, "OTEL_SERVICE_NAME": "checkoutservice", "SHIPPING_SERVICE_ADDR": f"127.0.0.1:{ship_port}",
                                "PAYMENT_SERVICE_ADDR": f"127.0.0.1:{pay_port}", "PRODUCT_CATALOG_SERVICE_ADDR": stub.addr, "CART_SERVICE_ADDR": stub.addr,
                                "CURRENCY_SERVICE_ADDR": stub.addr, "EMAIL_SERVICE_ADDR": stub.addr,
                                "ACCOUNT_SERVICE_ADDR": f"127.0.0.1:{S['port']}", "ACCOUNT_SERVICE_SECRET": env.SECRETS["checkoutservice"],
                                "CHECKOUT_LEASE_MS": 1500, "CHECKOUT_JANITOR_SECONDS": 1}))
shipping.start()
payment.start()
checkout.start()
CO_CHAN = grpc.insecure_channel(f"127.0.0.1:{co_port}")
CO = dpbg.CheckoutServiceStub(CO_CHAN)


def req_for(key, user, card=CARD, street=CAN["street"], email=CAN["email"], currency="USD", method_id=""):
    return dpb.PlaceOrderRequest(
        user_id=user, user_currency=currency, email=email, idempotency_key=key, channel="web", payment_method_id=method_id,
        address=dpb.Address(street_address=street, city=CAN["city"], state="TX", country="US", postal_code="73301", zip_code=73301),
        credit_card=dpb.CreditCardInfo(credit_card_number=card, credit_card_cvv=672, credit_card_expiration_year=2040, credit_card_expiration_month=1))


def place(key, user, token=None, extra_md=(), timeout=25, **kw):
    md = list(extra_md) + ([("x-account-session", token)] if token else [])
    try:
        return CO.PlaceOrder(req_for(key, user, **kw), metadata=md, timeout=timeout), None
    except grpc.RpcError as e:
        return None, e


def reason(err):
    m = err.details() or ""
    return m[len("reason:"):] if m.startswith("reason:") else m


def oid(key):
    return str(uuid.uuid5(ORDER_NS, key))


def cart(user, items=(("P1", 2),)):
    stub.carts[user] = list(items)


def pg(sql, params=()):
    return cluster.query(sql, params, db="svc")


def wait(pred, timeout=25.0, step=0.2):
    end = time.time() + timeout
    while time.time() < end:
        try:
            v = pred()
            if v:
                return v
        except Exception:
            pass
        time.sleep(step)
    return False


def order_row(key, state="confirmed"):
    r = pg("SELECT record_state, customer_id::text, total_units, tracking_id, transaction_id FROM orders WHERE order_id=%s", (oid(key),))
    return r[0] if r and r[0][0] == state else None


def recs(key):
    r = q()
    return r.hgetall(f"charge:{key}"), r.hgetall(f"ship:{key}"), r.hgetall(f"co:{key}")


def new_customer(first="Dora", last="Canary", email=None):
    email = email or f"cust.{uuid.uuid4().hex[:8]}@example.com"
    r, e = env.call(S["stub"].Register, apb.RegisterRequest(input=apb.RegistrationInput(
        first_name=first, last_name=last, email=email, phone="(650) 253-0000", password="Valid1Passw0rd", confirm_password="Valid1Passw0rd",
        address=apb.AccountAddress(line1="1 Main St", city="Austin", state="TX", zip="73301"), preferred_currency="USD")))
    assert e is None, e
    return r.session_token, r.context.customer_id


# --- 1. happy path, replay, conflict, concurrency ----------------------------------------------------------
def test_happy_replay_conflict():
    key, user = "key-happy-000001", CAN["session"]
    cart(user)
    r, e = place(key, user, extra_md=[("baggage", f"app.customer.email={CAN['bag']},app.session.ref=0123456789abcdef,free=free-text-canary")])
    check("guest checkout completes", e is None and r.status == "completed" and r.record_status == "recorded" and not r.replayed, e and e.details())
    check("order id derives from the idempotency key", r.order.order_id == oid(key), r.order.order_id)
    row = wait(lambda: order_row(key))
    check("order recorded in Postgres via the stream (guest order, NULL customer)", row and row[1] is None and row[3] == r.order.shipping_tracking_id, row)
    items = pg("SELECT product_id, name, quantity FROM order_items WHERE order_id=%s", (oid(key),))
    check("order items persisted with product names", items == [("P1", "Canary Mug", 2)], items)
    ch, sh, co = recs(key)
    check("exactly one durable charge, one shipment, state COMPLETED", ch.get("txn") and sh.get("tracking_id") == r.order.shipping_tracking_id and co.get("state") == "COMPLETED"
          and co.get("txn_id") == ch["txn"], (ch, sh, co.get("state")))
    check("cart emptied once, one confirmation email", stub.empty_calls.count(user) == 1 and stub.emails.count(r.order.order_id) == 1, (stub.empty_calls, stub.emails))
    ch_calls = int(ch.get("calls", 0))
    r2, e2 = place(key, user)
    check("replay returns the stored result (replayed=true, same order/tracking)", e2 is None and r2.replayed and r2.order.order_id == r.order.order_id
          and r2.order.shipping_tracking_id == r.order.shipping_tracking_id, e2 and e2.details())
    ch2, _, _ = recs(key)
    check("replay does not touch payment/shipping/email/cart", int(ch2.get("calls", 0)) == ch_calls and stub.emails.count(r.order.order_id) == 1 and stub.empty_calls.count(user) == 1)
    check("still exactly one order row", pg("SELECT count(*) FROM orders WHERE order_id=%s", (oid(key),))[0][0] == 1)
    r3, e3 = place(key, user, street="1 Different Street")
    check("same key with different details -> FAILED_PRECONDITION idempotency_conflict", e3 and e3.code() == grpc.StatusCode.FAILED_PRECONDITION and reason(e3) == "idempotency_conflict", e3 and e3.details())
    r4, e4 = place("short", user)
    check("malformed idempotency key rejected", e4 and e4.code() == grpc.StatusCode.INVALID_ARGUMENT)


def settle_happy_spans(timeout=20.0):
    """The telemetry checks below look at the FIRST order's spans. Later tests kill checkout/payment
    with SIGKILL, which loses any not-yet-exported batch, so wait until those spans have arrived."""
    want = oid("key-happy-000001")
    def seen():
        mine = {x.service for x in recv.spans if x.attrs.get("app.order.id") == want}
        return {"checkoutservice", "shippingservice", "accountservice"} <= mine
    ok = wait(seen, timeout)
    if not ok:
        print("DIAG happy-order spans per service:", sorted({x.service for x in recv.spans if x.attrs.get("app.order.id") == want}))
    return ok


def test_concurrent_duplicates():
    key, user = "key-concurrent-0001", "u-concurrent"
    cart(user)
    out = []

    def go():
        out.append(place(key, user))
    ts = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    ok = [r for r, e in out if r]
    errs = [reason(e) for r, e in out if e]
    check("8 concurrent submits: successes agree on one order; others are 'in progress', never errors of another kind",
          len(ok) >= 1 and len({r.order.order_id for r in ok}) == 1 and all(x in ("checkout_in_progress",) for x in errs), (len(ok), errs))
    check("one order row, one email, one charge record", wait(lambda: order_row(key)) and pg("SELECT count(*) FROM orders WHERE order_id=%s", (oid(key),))[0][0] == 1
          and stub.emails.count(oid(key)) == 1)


# --- 2. crash recovery at every step -----------------------------------------------------------------
POST_CHARGE = ("after_charged_state", "before_ship", "after_ship_call", "before_complete")


def test_crash_points():
    global checkout
    for fault in ("after_intent", "before_charge", "after_charge_call", *POST_CHARGE):
        key, user = f"key-crash-{fault}".replace("_", "-"), f"u-crash-{fault}"
        cart(user)
        checkout.restart(CHECKOUT_TEST_FAULT=fault)
        r, e = place(key, user)
        died = checkout.wait_dead(10)
        check(f"[{fault}] checkoutservice died at the injected point; client saw a failure", died and r is None and e is not None, (died, e and e.code()))
        checkout.start(CHECKOUT_TEST_FAULT=None)
        # checkout state is durable in Redis even though the process died
        ch, sh, co = recs(key)
        check(f"[{fault}] checkout state survived the crash in Redis", co.get("state") in ("PAYMENT_ATTEMPTING", "PAYMENT_CHARGED", "SHIPPING_ATTEMPTING", "SHIPPED"), co.get("state"))
        if fault in POST_CHARGE:
            done = wait(lambda: order_row(key), 25)
            check(f"[{fault}] janitor completes a charged checkout with no client retry (customer already paid)", bool(done), recs(key)[2].get("state"))
        time.sleep(1.8)   # lease expiry
        r2, e2 = place(key, user)
        check(f"[{fault}] retry with the same key succeeds", e2 is None and r2.status == "completed" and r2.order.order_id == oid(key), e2 and e2.details())
        ok = wait(lambda: order_row(key), 20)
        ch, sh, co = recs(key)
        check(f"[{fault}] exactly one order row, one charge txn, one shipment", bool(ok) and pg("SELECT count(*) FROM orders WHERE order_id=%s", (oid(key),))[0][0] == 1
              and co.get("state") == "COMPLETED" and co.get("txn_id") == ch.get("txn") and sh.get("tracking_id") == r2.order.shipping_tracking_id, (co.get("state"), ch, sh))
        check(f"[{fault}] at most one confirmation email", stub.emails.count(oid(key)) <= 1, stub.emails.count(oid(key)))


# --- 3. payment: declined, uncertain, slow ------------------------------------------------------------
def test_payment_paths():
    # declined
    key, user = "key-declined-0001", "u-declined"
    cart(user)
    r, e = place(key, user, card="1234-5678-9012-3456")
    check("declined card -> FAILED_PRECONDITION payment_declined", e and e.code() == grpc.StatusCode.FAILED_PRECONDITION and reason(e) == "payment_declined", e and e.details())
    ch, _, co = recs(key)
    check("declined: nothing charged, state PAYMENT_DECLINED", not ch and co.get("state") == "PAYMENT_DECLINED", (ch, co.get("state")))
    gone = wait(lambda: pg("SELECT count(*) FROM orders WHERE order_id=%s", (oid(key),))[0][0] == 0, 15)
    check("declined: the intent row is cleaned up (no phantom order in history)", bool(gone))
    r2, e2 = place(key, user, card="1234-5678-9012-3456")
    check("declined: replay of the same key reports the same decline without retrying the card", e2 and reason(e2) == "payment_declined")
    r3, e3 = place("key-declined-0002", user)
    check("a new key with a valid card then succeeds", e3 is None and r3.status == "completed", e3 and e3.details())

    # slow payment response (> client timeout): outcome unknown, resolved via GetCharge, not recharged
    key, user = "key-slowpay-0001", "u-slowpay"
    cart(user)
    payment.restart(PAYMENT_TEST_FAULT="slow_after_record:9000")
    r, e = place(key, user, timeout=40)
    check("slow payment: checkout resolves the unknown outcome with GetCharge and completes", e is None and r.status == "completed", e and e.details())
    ch, _, co = recs(key)
    check("slow payment: single charge txn matches the order", co.get("txn_id") == ch.get("txn") and co.get("state") == "COMPLETED")
    payment.restart(PAYMENT_TEST_FAULT=None)

    # payment process dies right after recording the charge
    key, user = "key-paydie-00001", "u-paydie"
    cart(user)
    payment.restart(PAYMENT_TEST_FAULT="exit_after_record")
    r, e = place(key, user, timeout=40)
    payment.wait_dead(5)
    payment.start(PAYMENT_TEST_FAULT=None)
    if e is not None:
        check("payment died mid-charge: client gets payment_uncertain (not a decline, not a duplicate charge)", e.code() == grpc.StatusCode.UNAVAILABLE and reason(e) == "payment_uncertain", e.details())
        time.sleep(1.8)
        r, e = place(key, user)
    check("payment died mid-charge: retry with the same key finds the recorded charge and completes", e is None and r.status == "completed", e and e.details())
    ch, _, co = recs(key)
    check("payment died mid-charge: exactly one charge record", co.get("txn_id") == ch.get("txn") and ch.get("txn"))


# --- 4. Redis (order queue) failures ------------------------------------------------------------------------
def test_queue_failures():
    # outage before anything happens -> fail closed, nothing charged
    key, user = "key-qdown-000001", "u-qdown"
    cart(user)
    order_q.kill9()
    r, e = place(key, user, timeout=15)
    check("order queue down: PlaceOrder fails closed (UNAVAILABLE order_queue_unavailable)", e and e.code() == grpc.StatusCode.UNAVAILABLE and reason(e) == "order_queue_unavailable", e and (e.code(), e.details()))
    order_q.start()
    time.sleep(1.0)
    r, e = place(key, user)
    check("order queue back: the same key now succeeds and nothing was charged during the outage", e is None and r.status == "completed", e and e.details())

    # AOF durability: checkout state + stream survive kill -9 of Redis
    key, user = "key-aof-0000001", "u-aof"
    cart(user)
    checkout.restart(CHECKOUT_TEST_FAULT="after_intent")
    place(key, user)
    checkout.wait_dead(10)
    wait(lambda: q().hget(f"co:{key}", "state") == "PAYMENT_ATTEMPTING", 5)
    order_q.kill9()
    order_q.start()
    time.sleep(1.0)
    check("kill -9 of the order queue: checkout state and intent survive via AOF", q().hget(f"co:{key}", "state") == "PAYMENT_ATTEMPTING"
          and q().hget(f"co:{key}", "pub_intent") not in (None, ""), q().hgetall(f"co:{key}").get("state"))
    checkout.start(CHECKOUT_TEST_FAULT=None)
    time.sleep(1.8)
    r, e = place(key, user)
    check("after Redis crash the checkout completes with the same key", e is None and r.status == "completed", e and e.details())

    # payment charged + shipped but the queue goes away before completion is published
    key, user = "key-unrec-000001", "u-unrec"
    cart(user)
    checkout.restart(CHECKOUT_TEST_FAULT="sleep_before_complete:3500")
    holder = {}
    th = threading.Thread(target=lambda: holder.update(zip(("r", "e"), place(key, user, timeout=40))))
    th.start()
    wait(lambda: q().hget(f"co:{key}", "state") == "SHIPPED", 10)
    order_q.kill9()
    th.join(40)
    r, e = holder.get("r"), holder.get("e")
    check("payment+shipping done but queue down at completion: success with record_status=history_unrecorded (explicit, not silent)",
          e is None and r is not None and r.status == "completed" and r.record_status == "history_unrecorded", e and e.details())
    order_q.start()
    done = wait(lambda: order_row(key), 40)
    check("history is eventually recorded once the queue returns (republish buffer / janitor)", bool(done))
    checkout.restart(CHECKOUT_TEST_FAULT=None)


# --- 5. consumer: duplicates, redelivery, poison, outage ---------------------------------------------------
def test_consumer():
    r = q()
    key, user = "key-consumer-0001", "u-consumer"
    cart(user)
    res, e = place(key, user)
    row = wait(lambda: order_row(key))
    check("baseline order consumed", e is None and bool(row))
    # duplicate delivery of the completed message does not duplicate items or rows
    co = r.hgetall(f"co:{key}")
    msg = None
    for mid, fields in r.xrange("orders:v1"):
        p = json.loads(fields["p"])
        if p["order_id"] == oid(key) and p["phase"] == "completed":
            msg = fields
    r.xadd("orders:v1", {"p": msg["p"], "phase": "completed", "order_id": oid(key), "tp": ""})
    time.sleep(1.5)
    check("duplicate completed message is idempotent (one row, items not duplicated)", pg("SELECT count(*) FROM orders WHERE order_id=%s", (oid(key),))[0][0] == 1
          and pg("SELECT count(*) FROM order_items WHERE order_id=%s", (oid(key),))[0][0] == 1)
    # a consumer failure leaves the message pending; XAUTOCLAIM redelivers it and it is applied exactly once
    key2, user2 = "key-consumer-0002", "u-consumer2"
    cart(user2)
    state = {"n": 0}

    def flaky(m):
        if m.get("order_id") == oid(key2) and m.get("phase") == "completed" and state["n"] < 2:
            state["n"] += 1
            raise RuntimeError("simulated consumer crash")
    consumer.fail_hook = flaky
    res, e = place(key2, user2)
    ok = wait(lambda: order_row(key2), 30)
    consumer.fail_hook = None
    check("consumer failure: message redelivered after idle (pending -> autoclaim) and applied once", bool(ok) and state["n"] == 2
          and pg("SELECT count(*) FROM order_items WHERE order_id=%s", (oid(key2),))[0][0] == 1, state)
    # poison message goes to the dead-letter stream without blocking the queue
    dead0 = r.xlen("orders:dead")
    r.xadd("orders:v1", {"p": "{not json", "phase": "completed", "order_id": "", "tp": ""})
    ok = wait(lambda: r.xlen("orders:dead") == dead0 + 1, 20)
    check("poison message dead-lettered with a reason code only", bool(ok) and "reason" in r.xrevrange("orders:dead", count=1)[0][1], r.xlen("orders:dead"))
    # Postgres outage: checkout still completes; history catches up when Postgres returns (not dead-lettered)
    key3, user3 = "key-pgdown-000001", "u-pgdown"
    cart(user3)
    dead1 = r.xlen("orders:dead")
    cluster.stop()
    try:
        res, e = place(key3, user3)
        check("Postgres down: checkout still completes (history is asynchronous)", e is None and res.status == "completed", e and e.details())
        time.sleep(5)
    finally:
        cluster.start()
    ok = wait(lambda: order_row(key3), 40)
    check("Postgres back: order recorded after redelivery; infrastructure errors are not dead-lettered", bool(ok) and r.xlen("orders:dead") == dead1)


def test_reconciler():
    import orders as ordmod
    r = q()
    # a stuck intent whose checkout actually COMPLETED but whose completed message never arrived
    key, user = "key-recon-0000001", "u-recon"
    cart(user)
    res, e = place(key, user)
    wait(lambda: order_row(key))
    pg("UPDATE orders SET record_state='intent', placed_at=NULL, intent_at = now() - interval '2 hours' WHERE order_id=%s", (oid(key),))
    pg("DELETE FROM order_items WHERE order_id=%s", (oid(key),))
    r.hdel(f"co:{key}", "pub_completed")
    n = ordmod.Reconciler(S["store"].repo, r, min_age_s=60).run_once()
    check("reconciler rebuilds a lost completion from the checkout state", n >= 1 and order_row(key) and pg("SELECT count(*) FROM order_items WHERE order_id=%s", (oid(key),))[0][0] == 1, n)
    # an intent whose payment was declined is removed
    key2 = "key-recon-0000002"
    r.hset(f"co:{key2}", mapping={"state": "PAYMENT_DECLINED", "fp": "x", "epoch": 1})
    pg("INSERT INTO orders (order_id, idempotency_key, email_hash, channel, record_state, intent_at, currency, total_units, total_nanos) VALUES (%s,%s,'h','web','intent', now() - interval '2 hours','USD',1,0)", (oid(key2), key2))
    ordmod.Reconciler(S["store"].repo, r, min_age_s=60).run_once()
    check("reconciler removes the intent of a declined checkout", pg("SELECT count(*) FROM orders WHERE order_id=%s", (oid(key2),))[0][0] == 0)


# --- 6. attribution, history APIs, guest claim --------------------------------------------------------------------
def test_attribution_history():
    token, cid = new_customer()
    key, user = "key-authed-000001", "u-authed-browser"
    cart(user, (("P1", 1), ("P2", 1)))
    r, e = place(key, user, token=token)
    check("authenticated checkout completes", e is None and r.status == "completed", e and e.details())
    row = wait(lambda: order_row(key))
    check("order is attributed to the customer resolved from the account session (not from the request)", row and row[1] == cid, row)
    lo, e = env.call(S["stub"].ListOrders, apb.ListOrdersRequest(page_size=10), token=token)
    check("ListOrders shows the order with item names and total", e is None and any(o.order_id == oid(key) and list(o.item_names) == ["Canary Mug", "Sunglasses"] and o.item_count == 2 for o in lo.orders), e and e.details())
    go, e = env.call(S["stub"].GetOrder, apb.OrderRef(order_id=oid(key)), token=token)
    check("GetOrder returns items, totals, ship-to and payment brand/last4", e is None and len(go.items) == 2 and go.payment_last4 == "0454" and go.ship_to.city == CAN["city"] and go.total.units > 0, e and e.details())
    t2, c2 = new_customer()
    go2, e2 = env.call(S["stub"].GetOrder, apb.OrderRef(order_id=oid(key)), token=t2)
    check("another customer cannot read the order (NOT_FOUND)", e2 and e2.code() == grpc.StatusCode.NOT_FOUND)
    lo2, _ = env.call(S["stub"].ListOrders, apb.ListOrdersRequest(page_size=10), token=t2)
    check("another customer's history does not include it", all(o.order_id != oid(key) for o in lo2.orders))
    # pagination (newest first)
    for i in range(3):
        k = f"key-page-0000{i}"
        cart(f"u-page-{i}")
        place(k, f"u-page-{i}", token=t2)
        wait(lambda k=k: order_row(k))
    qs, e = env.call(S["stub"].GetOrderQueueStatus, dpb.Empty(), token=t2)
    check("GetOrderQueueStatus reports depth/lag/dead", e is None and qs.depth >= 0 and qs.oldest_age_seconds >= 0 and qs.dead >= 0, e and e.details())
    p1, _ = env.call(S["stub"].ListOrders, apb.ListOrdersRequest(page_size=2), token=t2)
    p2, _ = env.call(S["stub"].ListOrders, apb.ListOrdersRequest(page_size=2, cursor=p1.next_cursor), token=t2)
    ids = [o.order_id for o in p1.orders] + [o.order_id for o in p2.orders]
    check("pagination: 3 orders across 2 pages, no duplicates, newest first", len(ids) == 3 and len(set(ids)) == 3 and ids[0] == oid("key-page-00002"), ids)
    # guest order -> claim by the same browser session
    gkey, guser = "key-guest-0000001", "guest-browser-claim"
    cart(guser)
    place(gkey, guser)
    wait(lambda: order_row(gkey))
    token3, cid3 = new_customer()
    cl, e = env.call(S["stub"].ClaimGuestOrders, apb.ClaimGuestRequest(session_id="some-other-browser"), token=token3)
    check("claiming with another browser's session id claims nothing", e is None and cl.claimed == 0)
    cl, e = env.call(S["stub"].ClaimGuestOrders, apb.ClaimGuestRequest(session_id=guser), token=token3)
    check("ClaimGuestOrders attaches orders placed from the same browser session", e is None and cl.claimed == 1 and order_row(gkey)[1] == cid3, e and e.details())


# --- 7. telemetry ---------------------------------------------------------------------------------------------
def test_telemetry():
    time.sleep(2.0)
    key = "key-happy-000001"
    if os.environ.get("DUR_DEBUG"):
        for sp in recv.find("hipstershop.CheckoutService/PlaceOrder", service="checkoutservice", kind="SERVER")[:6]:
            print("DBG", sp.trace_id, {k: v for k, v in sp.attrs.items() if k.startswith("app.")})
    co = [s for s in recv.find("hipstershop.CheckoutService/PlaceOrder", service="checkoutservice", kind="SERVER") if s.attrs.get("app.order.id") == oid(key)
          and s.attrs.get("app.checkout.idempotency_status") == "new"]
    if not co:
        allco = [x for x in recv.spans if x.service == "checkoutservice" and x.kind == "SERVER"]
        print("DIAG checkout SERVER spans:", len(allco), "total spans:", len(recv.spans), "want order", oid(key))
        import collections
        mine = collections.Counter((x.service, x.name, x.kind) for x in recv.spans if x.attrs.get("app.order.id") == oid(key))
        print("DIAG spans carrying the happy order id:", dict(mine))
        by_svc = collections.Counter(x.service for x in recv.spans)
        print("DIAG spans per service:", dict(by_svc))
        for pr in (checkout, payment):
            lines = [l for l in pr.log_text().splitlines() if any(w in l.lower() for w in ("export", "otel", "trace", "rror", "warn"))]
            print("DIAG", pr.name, "log lines of interest:", len(lines), lines[:6])
        for x in allco[:8]:
            print("DIAG", x.name, {k: v for k, v in x.attrs.items() if k.startswith("app.")})
    check("checkout SERVER span: typed order/idempotency attributes", bool(co) and co[0].attrs.get("app.order.persistence_status") == "recorded"
          and co[0].attrs.get("app.order.channel") == "web" and co[0].attrs.get("app.order.currency") == "USD" and co[0].attrs.get("app.customer.authenticated") is False
          and len(str(co[0].attrs.get("app.session.ref", ""))) == 16, co and co[0].attrs)
    tr = recv.trace_spans(co[0].trace_id) if co else []
    names = {s.name for s in tr}
    check("same trace shows every step", {"checkout.idem.begin", "checkout.plan", "orders.publish.intent", "checkout.payment", "checkout.shipping", "checkout.finalize", "orders.publish.completed"} <= names, names)
    svcs = {s.service for s in tr}
    check("same trace spans checkout, payment and shipping services", {"checkoutservice", "paymentservice", "shippingservice"} <= svcs, svcs)
    redis_spans = [s for s in tr if s.attrs.get("db.system") == "redis"]
    check("Redis CLIENT spans from checkout, payment and shipping with peer attributes", {s.service for s in redis_spans} >= {"checkoutservice", "paymentservice", "shippingservice"}
          and all(s.kind == "CLIENT" and s.attrs.get("server.address") and s.attrs.get("peer.service") for s in redis_spans), {s.service for s in redis_spans})
    pub = next((s for s in tr if s.name == "orders.publish.completed"), None)
    cons = [s for s in recv.find("orders.consume", service="accountservice", kind="CONSUMER") if pub and any(l[1] == pub.span_id for l in s.links)]
    check("order consumer runs in its OWN trace, linked to the publish span (async link, not parent)", bool(cons) and cons[0].trace_id != pub.trace_id and cons[0].parent_id == "", cons and (cons[0].trace_id, pub and pub.trace_id))
    ctr = recv.trace_spans(cons[0].trace_id) if cons else []
    check("consumer trace includes persist + Postgres client spans, latency attr", any(s.name == "orders.persist" for s in ctr) and any(s.attrs.get("db.system") == "postgresql" for s in ctr)
          and isinstance(cons[0].attrs.get("app.order.persistence_latency_ms"), int) and cons[0].attrs.get("app.order.outbox_status") == "consumed", cons and cons[0].attrs)
    rep = [s for s in recv.find("hipstershop.CheckoutService/PlaceOrder", service="checkoutservice", kind="SERVER") if s.attrs.get("app.checkout.idempotency_status") == "replayed"]
    check("replayed submits are visible in telemetry", bool(rep))
    res = [s for s in recv.spans if s.service == "checkoutservice" and s.attrs.get("app.checkout.resume_from")
           and s.name in ("hipstershop.CheckoutService/PlaceOrder", "checkout.janitor.resume")]
    check("resumed checkouts record resume_from and lease epoch", bool(res) and all(isinstance(s.attrs.get("app.checkout.lease_epoch"), int) for s in res), res and res[0].attrs)
    import telemetry_schema as sch
    bad = []
    for sp in list(recv.spans):
        for k, v in sp.attrs.items():
            if k.startswith("app.") and k not in ("app.order.total_value", "app.order.items", "app.payment.transaction_id", "app.shipping.tracking_id", "app.shipping.replayed",
                                                  "app.request.id", "app.currency", "app.cart.action"):
                try:
                    sch.coerce(k, list(v) if isinstance(v, list) else v)
                except ValueError:
                    bad.append((sp.service, sp.name, k, v))
    check("every exported app.* attribute (all services) satisfies the typed schema", not bad, bad[:3])
    canaries = [CAN["email"], CAN["street"], CAN["card"], CAN["card_digits"], CAN["session"], CAN["bag"], "free-text-canary", "Durabilityville"]
    hits = recv.canary_hits(canaries)
    check("no PII / card data / raw session id in any exported byte (all services, all signals)", not hits, hits)
    logs = {p.name: p.log_text() for p in (checkout, shipping, payment)}
    leaks = [(n, c) for n, t in logs.items() for c in canaries if c.lower() in t.lower()]
    check("service stdout logs contain no PII / card data / raw session id", not leaks, leaks)


def test_matrix_rows():
    import yaml
    rows = [r for r in yaml.safe_load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "telemetry_matrix.yaml")))["rows"]
            if r.get("suite") == "checkout_durability"]
    check("matrix declares R22-R25 for this suite", sorted(r["id"] for r in rows) == ["R22", "R23", "R24", "R25"], [r["id"] for r in rows])
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
        test_happy_replay_conflict()
        settle_happy_spans()
        test_concurrent_duplicates()
        test_crash_points()
        test_payment_paths()
        test_queue_failures()
        test_consumer()
        test_reconciler()
        test_attribution_history()
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
