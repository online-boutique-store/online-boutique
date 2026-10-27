"""CustomerUser (loadgenerator) against the REAL stack: Go frontend, accountservice (Postgres), checkoutservice,
paymentservice, shippingservice and the order-queue Redis. Locust runs headless; afterwards we assert the
journeys did what they claim (accounts created, saved card used, orders recorded, replays idempotent) and
that nothing sensitive was exported."""
import csv
import http.client
import json
import os
import subprocess
import sys
import tempfile
import time
import uuid

import grpc

import service_env as env
import stubs

PAY_PORT = stubs.free_port()
os.environ["PAYMENT_SERVICE_ADDR"] = f"127.0.0.1:{PAY_PORT}"
S = env.start()
import demo_pb2_grpc as dpbg  # noqa: E402,F401

recv, cluster, order_q = S["receiver"], S["cluster"], S["order_q"]
HERE = os.path.dirname(os.path.abspath(__file__))
SRC = stubs.SRC
LG_DIR = os.path.join(SRC, "loadgenerator")
LG_PY = os.environ.get("LOADGEN_PY", "/tmp/lgvenv2/bin/python")
FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {str(detail)[:300]}"))
    if not cond:
        FAILS.append(name)


def base_env(extra=None):
    e = {"ENABLE_TRACING": "1", "COLLECTOR_SERVICE_ADDR": f"127.0.0.1:{recv.port}", "OTEL_EXPORTER_OTLP_ENDPOINT": recv.endpoint,
         "ORDER_QUEUE_ADDR": f"127.0.0.1:{order_q.port}", "TELEMETRY_TEST_MODE": "1", "TELEMETRY_REF_KEY": "test-ref-key",
         "DISABLE_PROFILER": "1", "OTEL_RESOURCE_ATTRIBUTES": "service.namespace=online-boutique,service.version=test-1,deployment.environment.name=test",
         "OTEL_PROPAGATORS": "tracecontext,baggage", "OTEL_BSP_SCHEDULE_DELAY": "200", "OTEL_METRIC_EXPORT_INTERVAL": "500"}
    e.update(extra or {})
    return e


def main():
    if not os.path.exists(LG_PY):
        print("SKIP loadgen customer journeys: no loadgenerator venv at", LG_PY)
        return
    stubs.ensure_binaries()
    stub = stubs.Stubs()
    ship_port, co_port, fe_port = stubs.free_port(), stubs.free_port(), stubs.free_port()
    shipping = stubs.Proc("shipping", ["/tmp/shipping_bin"], os.path.join(SRC, "shippingservice"), ship_port,
                          base_env({"PORT": ship_port, "OTEL_SERVICE_NAME": "shippingservice"}))
    payment = stubs.Proc("payment", ["node", "index.js"], os.path.join(SRC, "paymentservice"), PAY_PORT,
                         base_env({"PORT": PAY_PORT, "OTEL_SERVICE_NAME": "paymentservice"}))
    checkout = stubs.Proc("checkout", ["/tmp/checkout_bin"], os.path.join(SRC, "checkoutservice"), co_port,
                          base_env({"PORT": co_port, "OTEL_SERVICE_NAME": "checkoutservice", "SHIPPING_SERVICE_ADDR": f"127.0.0.1:{ship_port}",
                                    "PAYMENT_SERVICE_ADDR": f"127.0.0.1:{PAY_PORT}", "PRODUCT_CATALOG_SERVICE_ADDR": stub.addr,
                                    "CART_SERVICE_ADDR": stub.addr, "CURRENCY_SERVICE_ADDR": stub.addr, "EMAIL_SERVICE_ADDR": stub.addr,
                                    "ACCOUNT_SERVICE_ADDR": f"127.0.0.1:{S['port']}", "ACCOUNT_SERVICE_SECRET": env.SECRETS["checkoutservice"]}))
    frontend = stubs.Proc("frontend", ["/tmp/fe_bin"], os.path.join(SRC, "frontend"), fe_port,
                          base_env({"PORT": fe_port, "LISTEN_ADDR": "127.0.0.1", "OTEL_SERVICE_NAME": "frontend", "OTEL_LOGS_EXPORTER": "none",
                                    "ACCOUNT_BACKEND": "grpc", "ACCOUNT_SERVICE_ADDR": f"127.0.0.1:{S['port']}",
                                    "ACCOUNT_SERVICE_SECRET": env.SECRETS["frontend"], "ENABLE_ASSISTANT": "false",
                                    "SHOPPING_ASSISTANT_SERVICE_ADDR": "127.0.0.1:1", "PRODUCT_CATALOG_SERVICE_ADDR": stub.addr,
                                    "CURRENCY_SERVICE_ADDR": stub.addr, "CART_SERVICE_ADDR": stub.addr,
                                    "RECOMMENDATION_SERVICE_ADDR": stub.addr, "AD_SERVICE_ADDR": stub.addr,
                                    "CHECKOUT_SERVICE_ADDR": f"127.0.0.1:{co_port}", "SHIPPING_SERVICE_ADDR": f"127.0.0.1:{ship_port}"}))
    procs = (shipping, payment, checkout, frontend)
    try:
        for p in procs:
            p.start()
        # the stub cart is keyed by the frontend session cookie; locust sends its own jar cookie, so seed per request instead
        out = tempfile.mkdtemp(prefix="lg")
        e = dict(os.environ)
        e.update({"CUSTOMER_IDENTITY_POOL": "3", "CUSTOMER_CHECKOUT_PROB": "1", "CUSTOMER_REPLAY_PROB": "0.5",
                  "CUSTOMER_THINK_MIN": "0.1", "CUSTOMER_THINK_MAX": "0.4", "CUSTOMER_PRODUCTS": "P1,P2",
                  "WEBSITE_USERS_WEIGHT": "0", "ASSISTANT_USERS_WEIGHT": "0", "CUSTOMER_USERS_WEIGHT": "1"})
        r = subprocess.run([LG_PY, "-m", "locust", "-f", "locustfile.py", "CustomerUser", "--headless", "-u", "3", "-r", "3",
                            "-t", "45s", "--host", f"http://127.0.0.1:{fe_port}", "--csv", os.path.join(out, "lg"), "--only-summary"],
                           cwd=LG_DIR, env=e, capture_output=True, text=True, timeout=300)
        rows = list(csv.DictReader(open(os.path.join(out, "lg_stats.csv"))))
        fails = list(csv.DictReader(open(os.path.join(out, "lg_failures.csv"))))
        by_name = {x["Name"]: x for x in rows}
        total = by_name.get("Aggregated", {})
        check("locust ran the CustomerUser journeys (requests issued)", int(total.get("Request Count", 0) or 0) > 30, r.stdout[-400:] + r.stderr[-400:])
        check("no failed requests in any customer journey", not fails and int(total.get("Failure Count", 0) or 0) == 0, fails[:3])
        for step in ("/account/create [customer]", "/login [customer]", "/account", "/account/orders", "/cart/checkout [customer]"):
            check(f"journey step exercised: {step}", step in by_name and int(by_name[step]["Request Count"]) > 0, sorted(by_name)[:12])

        q = lambda sql, p=(): cluster.query(sql, p, db="svc")  # noqa: E731
        custs = q("SELECT count(*) FROM customers WHERE email_display LIKE 'demo.webcustomer%%'")[0][0]
        check("accounts were created through the web signup form", custs >= 1, custs)
        pm = q("SELECT count(*) FROM payment_methods pm JOIN customers c ON c.id = pm.customer_id WHERE c.email_display LIKE 'demo.webcustomer%%'")[0][0]
        check("customers saved a demo payment method", pm >= 1, pm)
        deadline = time.time() + 20
        orders = 0
        while time.time() < deadline:
            orders = q("SELECT count(*) FROM orders o JOIN customers c ON c.id = o.customer_id WHERE c.email_display LIKE 'demo.webcustomer%%' "
                       "AND o.record_state = 'confirmed'")[0][0]
            if orders:
                break
            time.sleep(0.5)
        check("orders were recorded and attributed to the signed-in customers", orders >= 1, orders)
        saved = q("SELECT count(*) FROM orders o JOIN customers c ON c.id = o.customer_id WHERE c.email_display LIKE 'demo.webcustomer%%' "
                  "AND o.payment_method_type = 'demo_card_saved' AND o.payment_method_id IS NOT NULL")[0][0]
        check("customer orders were paid with the saved demo card", saved >= 1, saved)
        qc = order_q.client()
        dupes = q("SELECT count(*) FROM (SELECT idempotency_key FROM orders WHERE idempotency_key IS NOT NULL GROUP BY 1 HAVING count(*) > 1) d")[0][0]
        check("no idempotency key produced more than one order", dupes == 0, dupes)
        replays = [s for s in recv.spans if s.service == "checkoutservice" and s.kind == "SERVER"
                   and s.attrs.get("app.checkout.idempotency_status") == "replayed" and s.attrs.get("app.customer.authenticated") is True]
        check("replayed customer checkouts are visible in telemetry (idempotency_status=replayed)", bool(replays), len(replays))
        time.sleep(2)
        canaries = ["demo.webcustomer000@example.com", "demo.webcustomer001@example.com", "demo.webcustomer002@example.com",
                    "Valid1Passw0rd", "4432801561520454"]
        hits = recv.canary_hits(canaries)
        check("no customer email / password / card number in any exported telemetry byte", not hits, hits)
        logs = {p.name: p.log_text() for p in procs}
        leaks = [(n, c) for n, t in logs.items() for c in canaries if c.lower() in t.lower()]
        check("no customer email / password / card number in service logs", not leaks, leaks)
    finally:
        for p in procs:
            try:
                p.stop()
            except Exception:
                pass
        stub.stop()
    print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
