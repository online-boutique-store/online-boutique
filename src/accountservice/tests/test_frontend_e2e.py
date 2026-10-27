"""Frontend <-> accountservice end-to-end: the REAL Go frontend binary (built from src/frontend) talks
to a REAL accountservice (Postgres + Redis) while both export OTLP to a local receiver. Covers the
customer-visible account flows, security properties (CSRF, open redirect, cookies), degradation
during outages, trace continuity across both services, and PII/secret canaries over every exported
byte plus the frontend's own stdout. A second frontend with its sanitizer disabled (test mode) proves
the canary scan can fail."""
import http.client
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid

import service_env as env

S = env.start()
import accounts  # noqa: E402
import otel_harness as oh  # noqa: E402

recv, cluster = S["receiver"], S["cluster"]
FE_DIR = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "frontend"))
FAILS = []
CAN = {"first": "Zephyrine", "last": "Quillfeather", "email": "zq.web.canary@example.com", "phone": "(415) 555-2671",
       "street": "7741 Canarywood Boulevard", "city": "Cinderfall", "zip": "73301-4455", "password": "Canary-Pw-9x8y7z"}
GOOD = {"first_name": CAN["first"], "last_name": CAN["last"], "email": CAN["email"], "phone": CAN["phone"],
        "address_line1": CAN["street"], "address_line2": "", "city": CAN["city"], "state": "TX", "zip": CAN["zip"], "country": "US",
        "password": CAN["password"], "confirm_password": CAN["password"], "preferred_currency": "USD"}


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {str(detail)[:300]}"))
    if not cond:
        FAILS.append(name)


class Browser:
    def __init__(self, port):
        self.port, self.cookies, self.last = port, {}, None

    def req(self, method, path, form=None, headers=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        h = {"Cookie": "; ".join(f"{k}={v}" for k, v in self.cookies.items())}
        h.update(headers or {})
        body = None
        if form is not None:
            body = urllib.parse.urlencode(form)
            h["Content-Type"] = "application/x-www-form-urlencoded"
        conn.request(method, path, body=body, headers=h)
        r = conn.getresponse()
        data = r.read().decode("utf-8", "replace")
        for k, v in r.getheaders():
            if k.lower() == "set-cookie":
                name, _, rest = v.partition("=")
                val = rest.split(";", 1)[0]
                if "max-age=0" in v.lower() or "max-age=-1" in v.lower() or val == "":
                    self.cookies.pop(name, None)
                else:
                    self.cookies[name] = val
        self.last = (r.status, dict((k.lower(), v) for k, v in r.getheaders()), data, [v for k, v in r.getheaders() if k.lower() == "set-cookie"])
        return r.status, self.last[1], data

    def get(self, path, **kw):
        return self.req("GET", path, **kw)

    def csrf(self, path="/login"):
        self.get(path)
        return self.cookies.get("shop_csrf", "")

    def post(self, path, form, csrf_path="/login", with_csrf=True, headers=None):
        f = dict(form)
        if with_csrf:
            f["csrf"] = self.cookies.get("shop_csrf") or self.csrf(csrf_path)
        return self.req("POST", path, form=f, headers=headers)


def start_frontend(sanitizer_disabled=False):
    port = oh.free_port()
    e = dict(os.environ)
    e.update({"PORT": str(port), "LISTEN_ADDR": "127.0.0.1", "ENABLE_TRACING": "1", "COLLECTOR_SERVICE_ADDR": f"127.0.0.1:{recv.port}",
              "OTEL_SERVICE_NAME": "frontend", "OTEL_LOGS_EXPORTER": "none", "ACCOUNT_BACKEND": "grpc",
              "ACCOUNT_SERVICE_ADDR": S["channel_target"], "ACCOUNT_SERVICE_SECRET": env.SECRETS["frontend"],
              "TELEMETRY_REF_KEY": "test-ref-key", "OTEL_RESOURCE_ATTRIBUTES": "service.namespace=online-boutique,service.version=test-1,deployment.environment.name=test",
              "SHOPPING_ASSISTANT_SERVICE_ADDR": "127.0.0.1:1", "ENABLE_ASSISTANT": "false"})
    for k in ("PRODUCT_CATALOG_SERVICE_ADDR", "CURRENCY_SERVICE_ADDR", "CART_SERVICE_ADDR", "RECOMMENDATION_SERVICE_ADDR",
              "CHECKOUT_SERVICE_ADDR", "SHIPPING_SERVICE_ADDR", "AD_SERVICE_ADDR"):
        e[k] = "127.0.0.1:1"
    if sanitizer_disabled:
        e["TELEMETRY_SANITIZER_DISABLED"] = "1"
    log = tempfile.NamedTemporaryFile(prefix="fe", suffix=".log", delete=False)
    proc = subprocess.Popen(["/tmp/fe_bin"], cwd=FE_DIR, env=e, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
    for _ in range(100):
        try:
            c = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
            c.request("GET", "/_healthz")
            if c.getresponse().status == 200:
                return proc, port, log.name
        except OSError:
            time.sleep(0.1)
    raise RuntimeError("frontend did not start: " + open(log.name).read()[-500:])


def rows(sql, params=()):
    return cluster.query(sql, params, db="svc")


def flush(t=1.2):
    time.sleep(t)


def main():
    S["channel_target"] = f"127.0.0.1:{S['port']}"
    proc, port, logfile = start_frontend()
    b = Browser(port)
    try:
        run(b, port, logfile)
    finally:
        proc.terminate()
        proc.wait(5)
    print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'}")
    sys.exit(1 if FAILS else 0)


def run(b, port, logfile):
    # --- signup form + validation ---------------------------------------------------------------
    st, h, body = b.get("/account/create")
    states = re.findall(r'<option value="([A-Z]{2})"', body)
    check("signup page renders new fields and 51 state options", st == 200 and all(f in body for f in ("first_name", "phone", "address_line1", "zip", "confirm_password"))
          and len(set(states)) == 51, (st, len(states)))
    st, h, body = b.post("/account/create", dict(GOOD, email="nope", phone="555-1212", zip="12", state="XX", confirm_password="x"), "/account/create")
    check("invalid signup -> 422 with contract messages per field", st == 422 and "Enter a valid US phone number" not in body and "10-digit US phone" in body
          and "valid two-letter US state" in body and "5-digit ZIP" in body and "domain ending" in body or "valid email address" in body, (st, body[:200]))
    check("invalid signup keeps non-secret values, never the password", CAN["first"] in body and CAN["password"] not in body)
    check("no account created for invalid signup", rows("SELECT count(*) FROM customers WHERE email=%s", (CAN["email"],))[0][0] == 0)
    st, h, body = b.post("/account/create", GOOD, "/account/create", with_csrf=False)
    check("signup without CSRF -> 403", st == 403)
    st, h, body = b.post("/account/create", GOOD, "/account/create", headers={"Origin": "https://evil.example"})
    check("signup with foreign Origin -> 403", st == 403)

    # --- successful signup -> dashboard ------------------------------------------------------
    st, h, body = b.post("/account/create", GOOD, "/account/create")
    check("valid signup -> 302 /account and auth cookie", st == 302 and h["location"].endswith("/account") and "shop_account-session" in b.cookies, (st, h.get("location"), b.cookies.keys()))
    sc = [c for c in b.last[3] if c.startswith("shop_account-session=")][0].lower()
    check("auth cookie is HttpOnly + SameSite=Lax", "httponly" in sc and "samesite=lax" in sc, sc)
    st, h, body = b.get("/account")
    check("dashboard greets the customer, shows profile and default address", st == 200 and f"Welcome back, {CAN['first']}" in body and CAN["street"] in body
          and "(415) 555-2671" in body and "Complete your profile" not in body, (st, body[:300]))
    check("dashboard has orders + payment empty states", "No orders yet" in body and "No saved payment methods" in body)
    check("header shows greeting and Sign out", f"Hi, {CAN['first']}" in body and "/logout" in body)
    raw_session = b.cookies.get("shop_session-id", "")
    acct_token = b.cookies.get("shop_account-session", "")
    cid = rows("SELECT id::text FROM customers WHERE email=%s", (CAN["email"],))[0][0]
    check("session id cookie exists (raw value to be kept out of telemetry)", len(raw_session) > 10)

    # --- profile ---------------------------------------------------------------------------------
    st, h, body = b.get("/account/profile")
    check("profile page prefilled", st == 200 and 'value="Zephyrine"' in body and 'value="(415) 555-2671"' in body)
    st, h, body = b.post("/account/profile", {"first_name": "Zeph", "last_name": "Q", "phone": "555", "preferred_currency": "USD"}, "/account/profile")
    check("profile: invalid phone -> 422 with message", st == 422 and "10-digit US phone" in body)
    st, h, body = b.post("/account/profile", {"first_name": "Zeph", "last_name": "Quill-Feather", "phone": "650.253.0000", "preferred_currency": "EUR"}, "/account/profile")
    check("profile update -> 302", st == 302 and "/account" in h["location"], (st, h.get("location")))
    row = rows("SELECT first_name,last_name,phone_e164,preferred_currency FROM customers WHERE id=%s", (cid,))[0]
    check("profile persisted (E.164, currency)", row == ("Zeph", "Quill-Feather", "+16502530000", "EUR"), row)

    # --- addresses -------------------------------------------------------------------------------
    st, h, body = b.get("/account/addresses")
    check("address list shows the default address", st == 200 and CAN["street"] in body and "Default" in body)
    st, h, body = b.post("/account/addresses/new", {"label": "PO", "address_line1": "PO Box 77", "address_line2": "", "city": "Reno", "state": "NV", "zip": "89501", "country": "US"}, "/account/addresses/new")
    check("add address -> 302", st == 302, (st, body[:200]))
    st, h, body = b.post("/account/addresses/new", {"label": "", "address_line1": "1 Bad St", "address_line2": "", "city": "Reno", "state": "ZZ", "zip": "89501", "country": "US"}, "/account/addresses/new")
    check("add invalid address -> 422 with state message", st == 422 and "valid two-letter US state" in body)
    st, h, body = b.get("/account/addresses")
    aid = re.findall(r"/account/addresses/([0-9a-f-]{36})/default", body)
    check("non-default address offers Make default; PO Box badge", bool(aid) and "PO Box" in body, body[:200])
    st, h, body = b.get(f"/account/addresses/{aid[0]}/edit")
    check("edit form is prefilled", st == 200 and 'value="PO Box 77"' in body, st)
    st, h, body = b.post(f"/account/addresses/{aid[0]}/edit", {"label": "Annex", "address_line1": "PO Box 78", "address_line2": "", "city": "Reno", "state": "NV", "zip": "89502", "country": "US"}, "/account/addresses")
    check("edit address -> 302 and persisted", st == 302 and rows("SELECT line1, zip FROM customer_addresses WHERE id=%s", (aid[0],))[0] == ("PO Box 78", "89502"), st)
    st, h, body = b.post(f"/account/addresses/{aid[0]}/default", {}, "/account/addresses")
    st, h, body = b.get("/account/addresses")
    check("default switched", rows("SELECT is_default FROM customer_addresses WHERE id=%s", (aid[0],))[0][0] is True)
    st, h, body = b.post(f"/account/addresses/{aid[0]}/delete", {}, "/account/addresses")
    check("delete default promotes the remaining address", rows("SELECT count(*), bool_or(is_default) FROM customer_addresses WHERE customer_id=%s", (cid,))[0] == (1, True))
    st, h, body = b.post(f"/account/addresses/{aid[0]}/delete", {}, "/account/addresses", with_csrf=False)
    check("address delete without CSRF -> 403", st == 403)
    st, h, body = b.get(f"/account/addresses/{uuid.uuid4()}/edit")
    check("editing someone else's / unknown address -> 404 page", st == 404, st)

    # --- orders / payments panels ---------------------------------------------------------------
    st, h, body = b.get("/account/orders")
    check("orders page empty state", st == 200 and "haven't placed any orders" in body)
    st, h, body = b.get("/account/payment-methods")
    check("payment methods page empty state", st == 200 and "no saved payment methods" in body)
    check("payment page offers the demo catalog and has NO card-number / CVV input",
          body.count("Demo test card") >= 5 and "credit_card" not in body and 'name="cvv"' not in body and 'name="card' not in body)
    st, h, body = b.post("/account/payment-methods/add", {"catalog_id": "4532015112830366"}, "/account/payment-methods")
    check("posting a raw card number as catalog_id is refused (422, no echo)", st == 422 and "4532015112830366" not in body, st)
    st, h, body = b.post("/account/payment-methods/add", {"catalog_id": "demo_visa_1111"}, "/account/payment-methods")
    check("with paymentservice unreachable, add degrades to a 503 message (no stack, no crash)", st == 503 and "added right now" in body, st)
    st, h, body = b.post("/account/payment-methods/add", {"catalog_id": "demo_visa_1111"}, "/account/payment-methods", with_csrf=False)
    check("payment add without CSRF -> 403", st == 403, st)
    st, h, body = b.post(f"/account/payment-methods/{uuid.uuid4()}/delete", {}, "/account/payment-methods")
    check("deleting an unknown payment method -> 404 page", st == 404, st)
    st, h, body = b.post(f"/account/payment-methods/{uuid.uuid4()}/default", {}, "/account/payment-methods")
    check("defaulting an unknown payment method -> 404 page", st == 404, st)
    st, h, body = b.get(f"/account/orders/{uuid.uuid4()}")
    check("viewing an unknown / someone else's order -> 404 page", st == 404, st)

    # --- logout and login redirect ------------------------------------------------------------
    st, h, body = b.get("/logout")
    check("GET /logout only confirms", st == 200 and 'method="post"' in body and "shop_account-session" in b.cookies)
    st, h, body = b.post("/logout", {}, "/logout", with_csrf=False)
    check("POST /logout without CSRF -> 403 and still signed in", st == 403 and "shop_account-session" in b.cookies)
    st, h, body = b.post("/logout", {}, "/logout")
    check("POST /logout -> 302 home, cookie cleared", st == 302 and "shop_account-session" not in b.cookies)
    check("session deleted server-side", S["redis"].client().get("auth:" + accounts.hash_secret(acct_token)) is None)
    st, h, body = b.get("/account/orders")
    check("logged-out access redirects to login with next", st == 302 and h["location"].startswith("/login?next=") and "%2Faccount%2Forders" in h["location"], h.get("location"))
    st, h, body = b.post("/login", {"email": CAN["email"], "password": "Wrong-pass-1", "next": "/account/orders"})
    check("bad password -> 401 generic message, next preserved", st == 401 and "email or password is incorrect" in body and 'value="/account/orders"' in body)
    st, h, body = b.post("/login", {"email": CAN["email"].upper(), "password": CAN["password"], "next": "/account/orders"})
    check("login honours validated next", st == 302 and h["location"] == "/account/orders", (st, h.get("location")))
    b.post("/logout", {}, "/logout")
    st, h, body = b.post("/login", {"email": CAN["email"], "password": CAN["password"], "next": "https://evil.example/phish"})
    check("login ignores external next -> /account", st == 302 and h["location"] == "/account", h.get("location"))
    st, h, body = b.post("/login", {"email": CAN["email"], "password": CAN["password"], "next": "//evil.example"})
    check("login ignores protocol-relative next", st == 302 and h["location"] == "/account", h.get("location"))

    # --- activation of an account created in the assistant (handoff keyed by the browser session) ---
    import account_pb2 as pb
    ab = Browser(port)
    ab.get("/account/activation")
    check("activation page without a pending setup -> 404 message", ab.last[0] == 404 and "no account setup in progress" in ab.last[2], ab.last[0])
    sid = ab.cookies["shop_session-id"]
    reg = pb.RegistrationInput(first_name="Assia", last_name="Tant", email="assist.web@example.com", phone="(650) 253-0000",
                               address=pb.AccountAddress(line1="12 Pine Rd", city="Reno", state="NV", zip="89501"))
    r, e = env.call(S["stub"].BeginAssistantSignup, pb.AssistantSignupRequest(input=reg, session_id=sid), service="shoppingassistantservice")
    check("assistant-side signup created a pending account for this browser session", e is None and r.handoff, e)
    st, h, body = ab.get("/account/activation")
    check("activation page shows the pending email and a password form", st == 200 and "assist.web@example.com" in body and 'name="password"' in body, st)
    st, h, body = ab.post("/account/activation", {"password": "short"}, "/account/activation")
    check("weak password rejected with the existing rule message", st == 422 and "at least 8 characters" in body, (st, body[:120]))
    st, h, body = ab.post("/account/activation", {"password": "Activ4te-me-now"}, "/account/activation")
    check("activation completes: 302 /account and signed in", st == 302 and "shop_account-session" in ab.cookies, (st, h.get("location")))
    st, h, body = ab.get("/account")
    check("activated account dashboard greets Assia", st == 200 and "Welcome back, Assia" in body)
    CAN["assist_pw"] = "Activ4te-me-now"

    # --- legacy customer prompt ---------------------------------------------------------------
    lid, lemail = str(uuid.uuid4()), "legacy.web@example.com"
    cluster.query("INSERT INTO customers (id,email,name,status,created_via,password_hash) VALUES (%s,%s,'Lena Legacy','active','web',%s)", (lid, lemail, accounts._password_hash("Legacy-pass-1")), db="svc")
    cluster.query("UPDATE customers SET first_name='Lena', last_name='Legacy', email_display=email WHERE id=%s", (lid,), db="svc")
    cluster.query("INSERT INTO customer_addresses (customer_id,line1,city,state,zip,is_default) VALUES (%s,'9 Old Rd','Austin','TX','73301',true)", (lid,), db="svc")
    lb = Browser(port)
    st, h, body = lb.post("/login", {"email": lemail, "password": "Legacy-pass-1", "next": ""}, "/login")
    st, h, body = lb.get("/account")
    check("legacy customer sees the complete-profile banner", st == 200 and "Complete your profile" in body and "phone number" in body, body[:200])
    st, h, body = lb.post("/account/profile", {"first_name": "Lena", "last_name": "Legacy", "phone": "(512) 555-0100", "preferred_currency": "USD"}, "/account/profile")
    st, h, body = lb.get("/account")
    check("banner disappears after completing the profile", "Complete your profile" not in body)

    # --- outage: Postgres down, session survives, panels degrade ------------------------------------
    cluster.stop()
    try:
        st, h, body = b.get("/account")
        check("Postgres outage: dashboard still renders 200, panels say unavailable, still signed in",
              st == 200 and "temporarily unavailable" in body and "Sign out" in body, (st, body[:200]))
        st, h, body = b.get("/account/addresses")
        check("Postgres outage: addresses page -> 503 'try again', no logout", st == 503 and "shop_account-session" in b.cookies, st)
        st, h, body = b.post("/login", {"email": "someone@example.com", "password": "Whatever-1", "next": ""}, "/login")
        check("Postgres outage: login -> 503 friendly message", st == 503 and "temporarily unavailable" in body, st)
    finally:
        cluster.start()
    for _ in range(40):
        if b.get("/account")[0] == 200 and "Welcome back" in b.last[2]:
            break
        time.sleep(0.5)
    check("dashboard recovers after Postgres returns", "Welcome back" in b.last[2])

    # --- telemetry -------------------------------------------------------------------------------
    pii_baggage = (("baggage"), "app.customer.email=baggage.web.canary%40example.com,app.session.ref=0123456789abcdef,free=free-web-canary")
    b.get("/account", headers={"baggage": pii_baggage[1]})
    flush()
    fe = recv.find("frontend", service="frontend", kind="SERVER")
    dash = [s for s in fe if s.attrs.get("app.account.action") == "dashboard_view" and s.attrs.get("app.customer.id") == cid]
    check("frontend SERVER span: typed account attributes", bool(dash) and dash[0].attrs.get("app.customer.authenticated") is True and dash[0].attrs.get("app.customer.type") == "authenticated"
          and dash[0].attrs.get("app.customer.profile_complete") is True and re.fullmatch(r"[0-9a-f]{16}", str(dash[0].attrs.get("app.session.ref", ""))), dash and dash[0].attrs)
    tr = recv.trace_spans(dash[0].trace_id) if dash else []
    acct_servers = [s for s in tr if s.service == "accountservice" and s.kind == "SERVER"]
    check("one trace spans frontend and accountservice (profile, addresses, orders, payments)", {s.name.split("/")[-1] for s in acct_servers} >= {"GetProfile", "ListAddresses", "ListOrders", "ListPaymentMethods"}, [s.name for s in tr])
    cmap = {s.span_id: s for s in tr}
    check("accountservice SERVER spans are children of frontend gRPC CLIENT spans", all(cmap.get(s.parent_id) and cmap[s.parent_id].kind == "CLIENT" and cmap[s.parent_id].service == "frontend" for s in acct_servers))
    check("account.dashboard INTERNAL span groups the panel calls", any(s.name == "account.dashboard" for s in tr))
    check("accountservice spans in that trace carry the customer id from the session", all(s.attrs.get("app.customer.id") == cid for s in acct_servers), [s.attrs.get("app.customer.id") for s in acct_servers])
    lg = [s for s in recv.find("/hipstershop.AccountService/Login", service="accountservice", kind="SERVER") if s.attrs.get("app.customer.id") == cid]
    check("login traced end to end (frontend -> accountservice -> postgres)", bool(lg) and any(x.attrs.get("db.system") == "postgresql" for x in recv.trace_spans(lg[0].trace_id)))
    post_spans = [s for s in fe if s.attrs.get("app.account.action") == "login" and s.attrs.get("app.account.outcome") in ("success", "failed")]
    check("frontend login spans record outcome only", {s.attrs.get("app.account.outcome") for s in post_spans} >= {"success", "failed"}, [s.attrs for s in post_spans][:2])
    check("validation failure span lists field names and codes only", any(s.attrs.get("app.validation.fields") and "phone" in s.attrs["app.validation.fields"] for s in fe), None)
    check("baggage: PII/free-text dropped, ref accepted", not recv.canary_hits(["baggage.web.canary", "free-web-canary"]))
    check("services share resource attributes (namespace, version)", {s.resource.get("service.namespace") for s in recv.spans if s.service in ("frontend", "accountservice")} == {"online-boutique"})

    import yaml
    mx = yaml.safe_load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "telemetry_matrix.yaml")))["frontend"]
    seen = {s.attrs.get("app.account.action") for s in recv.find("frontend", service="frontend", kind="SERVER")}
    for r in [r for r in mx if r["status"] == "implemented"]:
        check(f"matrix {r['id']} {r['route']}: frontend span with app.account.action={r['action']}", r["action"] in seen, sorted(x for x in seen if x))
    canaries = list(CAN.values()) + [CAN["first"].lower(), "(650) 253-0000", "650.253.0000", "6502530000", "+16502530000", "Quill-Feather", "PO Box 77", "legacy.web@example.com", "Legacy-pass-1",
                                      acct_token, raw_session, b.cookies.get("shop_csrf", ""), "Wrong-pass-1", "Lena", "9 Old Rd", "512", "assist.web@example.com", "Activ4te-me-now", "12 Pine Rd", ab.cookies.get("shop_session-id", ""), ab.cookies.get("shop_account-session", "")]
    canaries = [c for c in canaries if c and len(c) >= 5]
    hits = recv.canary_hits(canaries)
    check("no PII / secret / token / raw session id in any exported span, event, link, resource or metric", not hits, hits)
    logs = open(logfile).read()
    log_hits = [c for c in canaries if c.lower() in logs.lower()]
    check("frontend stdout logs contain no PII / secrets / raw session id", not log_hits, log_hits)
    check("frontend logs use the opaque session reference", "session_ref" in logs and raw_session not in logs)

    # --- negative control: frontend sanitizer disabled (test mode) leaks ---------------------------
    probe_email = "probe.fe.canary@example.com"
    probe_ua = "Mozilla/5.0 (probe 4412 Hollowbrook Avenue) ua.canary@example.net"
    c1 = Browser(port)
    c1.get(f"/account/create?probe={probe_email}", headers={"User-Agent": probe_ua})
    flush()
    on_hits = recv.canary_hits([probe_email, "ua.canary@example.net", "4412 Hollowbrook Avenue"])
    check("sanitizer ON: query string and UA canaries not exported", not on_hits, on_hits)
    proc2, port2, log2 = start_frontend(sanitizer_disabled=True)
    try:
        c2 = Browser(port2)
        c2.get(f"/account/create?probe={probe_email.replace('probe.fe', 'probe.off')}", headers={"User-Agent": probe_ua.replace("ua.canary", "ua.off")})
        flush(1.5)
        off_hits = recv.canary_hits(["probe.off.canary@example.com", "ua.off@example.net", "ua.off.canary@example.net"])
        check("negative control: frontend sanitizer OFF leaks the query-string / UA canaries", bool(off_hits), off_hits)
    finally:
        proc2.terminate()
        proc2.wait(5)


if __name__ == "__main__":
    main()
