"""Assistant account tools with ACCOUNT_BACKEND=grpc, against a REAL accountservice subprocess
(Postgres + Redis). Proves the assistant no longer validates or stores accounts itself: phone and
address line 2 are collected, server field errors are relayed by contract message without echoing
values, activation stays server-side, and email enumeration is not possible through lookup_account."""
import json
import os
import socket
import subprocess
import sys
import time
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

stub = types.ModuleType("backends")
stub.cart_contents = lambda uid: ([{"product_id": "P1", "name": "Mug", "quantity": 1, "unit_price_usd": 8.0}], 8.0)
stub.place_order = lambda *a, **k: {"order_id": "ord-1"}
stub.TOOLS = {}
sys.modules["backends"] = stub

import pgtest  # noqa: E402

ACC_DIR = os.path.join(os.path.dirname(os.path.dirname(HERE)), "accountservice")
SECRET = "assistant-test-secret"
FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {str(detail)[:300]}"))
    if not cond:
        FAILS.append(name)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def start_redis():
    port = free_port()
    proc = subprocess.Popen(["redis-server", "--port", str(port), "--bind", "127.0.0.1", "--save", "", "--appendonly", "no"],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    import redis
    for _ in range(100):
        try:
            redis.Redis(port=port, socket_connect_timeout=0.2).ping()
            return proc, port
        except Exception:
            time.sleep(0.05)
    raise RuntimeError("redis")


def start_accountservice(dsn, redis_port):
    port = free_port()
    env = dict(os.environ, PORT=str(port), ACCOUNTS_DATABASE_URL=dsn, REDIS_ADDR=f"127.0.0.1:{redis_port}",
               ACCOUNT_SERVICE_SECRETS=f"shoppingassistantservice:{SECRET}", ENABLE_TRACING="0", TELEMETRY_REF_KEY="k")
    proc = subprocess.Popen([sys.executable, "server.py"], cwd=ACC_DIR, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    time.sleep(1.0)
    return proc, port


def main():
    cluster = pgtest.cluster()
    cluster.create_db("asst")
    rproc, rport = start_redis()
    sproc, sport = start_accountservice(cluster.dsn("asst"), rport)
    try:
        run(cluster, rport, sport)
    finally:
        sproc.terminate()
        rproc.terminate()
    print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'}")
    sys.exit(1 if FAILS else 0)


def run(cluster, rport, sport):
    os.environ["ACCOUNT_BACKEND"] = "grpc"
    import accounts
    import account_client
    import checkout
    import config
    import incident

    config.ACCOUNT_BACKEND = "grpc"
    client = account_client.AccountClient(addr=f"127.0.0.1:{sport}", secret=SECRET)
    accounts.set_grpc_client(client)
    for _ in range(60):
        try:
            client.validate({"email": "a@example.com"})
            break
        except Exception:
            time.sleep(0.5)

    class Rec(checkout.Hooks):
        def __init__(self):
            self.events = []

        def event(self, name, st, **attrs):
            self.events.append((name, attrs))

    class Sess:
        def __init__(self, sid):
            self.id = sid
            self.checkout = checkout.CheckoutState()
            self.checkout.session_id = sid
            self.customer_account = None

    rec = Rec()
    checkout.hooks = rec
    good = dict(first_name="Zephyrine", last_name="Quillfeather", email="Zq.Assist@Example.com", phone="(415) 555-2671",
                street_address="7741 Canarywood Boulevard", address_line2="Suite 9Q", city="Cinderfall", state="tx", zip_code="73301-4455", country="")

    # tool schema reflects the contract
    tools = {t["function"]["name"]: t["function"]["parameters"] for t in incident.tool_definitions()}
    ca = tools["create_account"]
    check("create_account tool requires phone and no longer requires country; line2 optional",
          "phone" in ca["required"] and "country" not in ca["required"] and "address_line2" in ca["properties"] and "address_line2" not in ca["required"], ca["required"])
    check("set_shipping_address: country/line2 optional", tools["set_shipping_address"]["required"] == ["street_address", "city", "state", "zip_code"])
    for v in ("v1", "v2"):
        config.PROMPT_VERSION = v
        check(f"prompt {v}: lists phone number and ZIP code as required", "phone number" in incident.system_prompt() and "ZIP code" in incident.system_prompt())
    config.PROMPT_VERSION = "v1"

    # server-side validation errors are relayed, not decided locally
    s = Sess("sess-asst-1")
    r = checkout.create_account(s, **dict(good, phone="555-1212", state="ZZ", email="not-an-email"))
    check("invalid fields -> needs_info naming phone, state, email", r["status"] == "needs_info" and set(r["missing_fields"]) >= {"phone number", "state", "email address"}, r)
    check("field_errors use contract messages and never echo input",
          "10-digit US phone" in json.dumps(r["field_errors"]) and "555-1212" not in json.dumps(r) and "not-an-email" not in json.dumps(r) and "ZZ" not in json.dumps(r), r.get("field_errors"))
    r = checkout.create_account(s, **dict(good, first_name="", last_name=""))
    check("missing names listed", r["status"] == "needs_info" and {"first name", "last name"} <= set(r["missing_fields"]), r)
    check("nothing stored for rejected attempts", cluster.query("SELECT count(*) FROM customers", db="asst")[0][0] == 0)

    # success -> pending account + handoff, address normalized by the server
    r = checkout.create_account(s, **good)
    check("create_account succeeds with activation handoff", r.get("status") == "account_created" and r.get("complete_setup") is True, r)
    row = cluster.query("SELECT email, email_display, phone_e164, status, created_via, profile_version, first_name, last_name FROM customers", db="asst")
    check("pending v2 customer persisted with E.164 phone and display email", row == [("zq.assist@example.com", "Zq.Assist@example.com", "+14155552671", "pending_activation", "assistant", 2, "Zephyrine", "Quillfeather")], row)
    addr = cluster.query("SELECT line1, line2, state, zip FROM customer_addresses", db="asst")
    check("address stored normalized (state upper-case, ZIP+4)", addr == [("7741 Canarywood Boulevard", "Suite 9Q", "TX", "73301-4455")], addr)
    check("checkout state holds the server-normalized address", s.checkout.address["state"] == "TX" and s.checkout.address["zip_code"] == "73301-4455" and "Suite 9Q" in s.checkout.address["street_address"], s.checkout.address)
    act = client.activation("sess-asst-1")
    check("activation pending for this browser session only", act["pending"] and not client.activation("other-session")["pending"])
    check("raw activation code never returned to the assistant", "raw_code" not in json.dumps(r) and not any("code" in k for k in r))

    # duplicates / enumeration
    s2 = Sess("sess-asst-2")
    r = checkout.create_account(s2, **good)
    check("same email from another session -> pending_elsewhere (no detail leak)", r.get("status") == "account_pending_elsewhere", r)
    r = checkout.lookup_account(s2, "zq.assist@example.com")
    check("lookup_account while signed out never confirms an account", r.get("found") is False and r.get("signed_in") is False and "exists" not in json.dumps(r).replace("do not say whether an account exists", "").lower().replace("whether an account exists for an email", ""), r)

    # signed-in customer prefill comes from the session
    s3 = Sess("sess-asst-3")
    s3.customer_account = {"customer_id": "11111111-2222-4333-8444-555555555555", "name": "Ada Lovelace", "email": "ada@example.com",
                           "address": {"street_address": "1 Main St", "city": "Austin", "state": "TX", "zip_code": "73301", "country": "US"}}
    r = checkout.lookup_account(s3, "someone-else@example.com")
    check("signed-in lookup uses the session identity, ignores the typed email", r.get("found") is True and s3.checkout.email == "ada@example.com" and s3.checkout.address["city"] == "Austin", r)

    # shipping + guest paths use the server too
    s4 = Sess("sess-asst-4")
    r = checkout.set_shipping_address(s4, street_address="1 Elm St", city="Reno", state="NV", zip_code="1234")
    check("shipping address validated by the server (bad ZIP)", r["status"] == "needs_info" and "ZIP code" in r["missing_fields"], r)
    r = checkout.set_shipping_address(s4, street_address="PO Box 7", city="Reno", state="nv", zip_code="89501", address_line2="")
    check("shipping address saved normalized", r["status"] == "address_saved" and s4.checkout.address["state"] == "NV" and s4.checkout.address["country"] == "US", r)
    r = checkout.continue_as_guest(s4, "guest@b.c")
    check("guest email validated by the server (one-letter TLD rejected)", r["status"] == "needs_info", r)
    r = checkout.continue_as_guest(s4, "Guest@Example.COM")
    check("guest email accepted (server-normalized domain)", r["status"] == "guest" and s4.checkout.email == "Guest@example.com", (r, s4.checkout.email))

    # outage: accountservice down -> friendly error, no local fallback validation
    sproc_dead = account_client.AccountClient(addr="127.0.0.1:1", secret=SECRET, timeout=2)
    accounts.set_grpc_client(sproc_dead)
    s5 = Sess("sess-asst-5")
    r = checkout.create_account(s5, **good)
    check("accountservice unreachable -> error + guest hint (no local fallback)", "error" in r and "guest" in r.get("hint", "").lower(), r)


if __name__ == "__main__":
    main()
