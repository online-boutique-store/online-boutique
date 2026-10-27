"""Local end-to-end integration: REAL Go frontend + REAL Python assistant process + real Postgres
(bundled cluster) + real redis-server. Only the gRPC backends and Bedrock are the scripted fakes
from fakes.py (the assistant process talks to them over the network like it would in the cluster).

Run (from src/shoppingassistantservice):  python tests/integration_local.py
Needs: redis-server on PATH, Go toolchain, the pgserver venv. Nothing leaves localhost.
"""
import http.cookiejar
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
SVC = os.path.dirname(HERE)
FRONTEND = os.path.join(os.path.dirname(SVC), "frontend")
sys.path[:0] = [HERE, SVC]

import pgtest  # noqa: E402
import fakes  # noqa: E402

SECRET = "integration-internal-secret"
PW = "Sup3rPassw0rd!"
results = []


def check(cond, label):
    results.append((bool(cond), label))
    print(("PASS  " if cond else "FAIL  ") + label, flush=True)


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_http(url, timeout=60):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with urllib.request.urlopen(url, timeout=2) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code
        except Exception:  # noqa: BLE001
            time.sleep(0.3)
    raise RuntimeError(f"timeout waiting for {url}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


class Browser:
    def __init__(self, base):
        self.base, self.jar = base, http.cookiejar.CookieJar()
        self.op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar), NoRedirect)

    def req(self, method, path, form=None, js=None, headers=None):
        data, hdrs = None, dict(headers or {})
        if form is not None:
            data = urllib.parse.urlencode(form).encode()
            hdrs["Content-Type"] = "application/x-www-form-urlencoded"
        if js is not None:
            data = json.dumps(js).encode()
            hdrs["Content-Type"] = "application/json"
        r = urllib.request.Request(self.base + path, data=data, method=method, headers=hdrs)
        try:
            with self.op.open(r, timeout=30) as resp:
                return resp.status, resp.headers, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode()

    def cookie(self, name):
        return next((c.value for c in self.jar if c.name == name), None)

    def cookies(self):
        return {c.name: c.value for c in self.jar}

    def csrf(self, path):
        _, _, html = self.req("GET", path)
        m = re.search(r'name="csrf" value="([^"]+)"', html)
        return m.group(1) if m else ""

    def chat(self, text, category="test"):
        code, _, body = self.req("POST", "/bot", js={"message": text}, headers={"X-Prompt-Category": category})
        try:
            return code, json.loads(body)
        except ValueError:
            return code, {"raw": body}


def main():
    procs, tmp = [], tempfile.mkdtemp(prefix="obint")
    pg = pgtest.cluster()
    redis_port = free_port()
    procs.append(subprocess.Popen(["redis-server", "--port", str(redis_port), "--bind", "127.0.0.1",
                                   "--save", "", "--appendonly", "no", "--dir", tmp],
                                  stdout=open(os.path.join(tmp, "redis.log"), "w"), stderr=subprocess.STDOUT,
                                  stdin=subprocess.DEVNULL))
    fakes.start_backends()
    fakes.start_fake_bedrock()
    import redis as redislib
    rds = redislib.Redis(host="127.0.0.1", port=redis_port, decode_responses=True)
    for _ in range(50):
        try:
            rds.ping()
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.1)

    a_port, f_port = free_port(), free_port()
    a_env = {k: v for k, v in os.environ.items() if not k.startswith(("OTEL_", "ENABLE_TRACING"))}
    a_env.update({
        "PORT": str(a_port), "REDIS_ADDR": f"127.0.0.1:{redis_port}", "ACCOUNTS_DATABASE_URL": pg.dsn(),
        "ACCOUNT_API_SECRET": SECRET, "RATE_LIMIT_RPM": "0", "PROMPT_VERSION": "v1",
        "ACCOUNTS_MIGRATION_RETRY_MAX_S": "2", "PYTHONUNBUFFERED": "1"})
    a_log = os.path.join(tmp, "assistant.log")
    procs.append(subprocess.Popen([sys.executable, "shoppingassistantservice.py"], cwd=SVC, env=a_env,
                                  stdout=open(a_log, "w"), stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL))
    binary = os.path.join(tmp, "frontend")
    subprocess.run(["go", "build", "-o", binary, "."], cwd=FRONTEND, check=True)
    f_env = dict(os.environ)
    f_env.update({
        "PORT": str(f_port), "ENABLE_ASSISTANT": "true", "ACCOUNT_API_SECRET": SECRET,
        "SHOPPING_ASSISTANT_SERVICE_ADDR": f"127.0.0.1:{a_port}",
        "PRODUCT_CATALOG_SERVICE_ADDR": os.environ["PRODUCT_CATALOG_SERVICE_ADDR"],
        "CART_SERVICE_ADDR": os.environ["CART_SERVICE_ADDR"],
        "CURRENCY_SERVICE_ADDR": os.environ["CURRENCY_SERVICE_ADDR"],
        "CHECKOUT_SERVICE_ADDR": os.environ["CHECKOUT_SERVICE_ADDR"],
        "RECOMMENDATION_SERVICE_ADDR": "127.0.0.1:1", "SHIPPING_SERVICE_ADDR": "127.0.0.1:1",
        "AD_SERVICE_ADDR": "127.0.0.1:1", "ENABLE_TRACING": "0", "ENABLE_PROFILER": "0"})
    f_log = os.path.join(tmp, "frontend.log")
    procs.append(subprocess.Popen([binary], cwd=FRONTEND, env=f_env, stdout=open(f_log, "w"),
                                  stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL))
    base = f"http://127.0.0.1:{f_port}"
    try:
        wait_http(f"http://127.0.0.1:{a_port}/healthz")
        end = time.monotonic() + 30
        while time.monotonic() < end and wait_http(f"http://127.0.0.1:{a_port}/readyz/accounts") != 200:
            time.sleep(0.5)
        check(wait_http(f"http://127.0.0.1:{a_port}/readyz/accounts") == 200, "assistant process migrated Postgres and is ready")
        wait_http(base + "/_healthz")
        run_flows(base, pg, rds)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        check(False, "integration harness error")
    finally:
        for p in procs:
            p.terminate()
        for p in procs:
            try:
                p.wait(10)
            except Exception:  # noqa: BLE001
                p.kill()
        logs = open(a_log).read() + open(f_log).read()
        check(PW not in logs, "assistant + frontend process logs never contain the password")
        for secret in LEAK_CHECK:
            check(secret not in logs, "process logs never contain the raw activation code")
        print(f"\n(logs kept in {tmp})")
        pg.close()
    failed = [l for ok, l in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
    sys.exit(1 if failed else 0)


LEAK_CHECK = []


def run_flows(base, pg, rds):
    # 1. Web signup through the real frontend form -> Postgres -> Redis session -> auth cookie
    a = Browser(base)
    csrf = a.csrf("/account/create")
    code, hdrs, html = a.req("POST", "/account/create", form={
        "csrf": csrf, "first_name": "Web", "last_name": "Shopper", "email": "Web.Shopper@example.com", "password": PW,
        "street_address": "1 Main St", "city": "Austin", "state": "TX", "zip_code": "73301",
        "country": "United States", "preferred_currency": ""})
    check(code == 302 and hdrs.get("Location", "").endswith("/account/profile") and a.cookie("shop_account-session"),
          f"web signup via frontend form -> 302 profile + auth cookie (got {code}, {re.findall(r'account-error[^<]*<', html)[:1]})")
    code, _, html = a.req("GET", "/account/profile")
    check(code == 200 and 'value="Web"' in html and 'value="Shopper"' in html, f"profile page renders the Postgres-backed account (got {code}: {html[:80]!r})")
    row = pg.query("SELECT email, status, created_via, password_hash IS NOT NULL FROM customers WHERE email='web.shopper@example.com'")
    check(row == [("web.shopper@example.com", "active", "web", True)], f"Postgres row for web signup (email lower-cased) {row}")
    import hashlib
    tok = a.cookie("shop_account-session") or ""
    check(rds.ttl("auth:" + hashlib.sha256(tok.encode()).hexdigest()) > 0 and not rds.exists("auth:" + tok),
          "session stored in real Redis under a hash of the token (raw token is not a key)")
    # logout / login
    a.req("GET", "/logout")
    check(not a.cookie("shop_account-session"), "logout clears the auth cookie")
    csrf = a.csrf("/login")
    code, hdrs, _ = a.req("POST", "/login", form={"csrf": csrf, "email": "web.shopper@example.com", "password": PW})
    check(code == 302 and a.cookie("shop_account-session"), "login through the frontend")
    csrf = a.csrf("/login")
    code, _, html = Browser(base).req("POST", "/login", form={"csrf": "x", "email": "web.shopper@example.com", "password": "nope"})
    check(code in (403, 200) and "account-session" not in str(code), "bad CSRF/password does not log in")

    # 2. Chat account creation -> server-side activation through the real frontend
    b = Browser(base)
    b.req("GET", "/")  # obtain the shop session cookie
    code, r1 = b.chat("I'd like to check out", "checkout_request")
    code2, r2 = b.chat("my email is chat.int@example.com", "checkout_request")
    check(code == 200 and code2 == 200 and "Account created" in r2.get("message", ""), f"chat creates a pending account ({r2.get('message', r2)})")
    check(bool(r2.get("complete_setup") or (r2.get("checkout") or {}).get("complete_setup")), f"chat response asks the browser to complete setup ({ {k: v for k, v in r2.items() if k != 'message'} })")
    handoffs = [k for k in rds.keys("activation_handoff:*")]
    check(len(handoffs) == 1 and 0 < rds.ttl(handoffs[0]) <= 900, "one activation handoff in Redis with TTL <= 900s")
    raw_code = json.loads(rds.get(handoffs[0]))["raw_code"] if handoffs else "missing"
    LEAK_CHECK.append(raw_code)
    code, _, html = b.req("GET", "/account/activation")
    check(code == 200 and "chat.int@example.com" in html, "activation page shows the pending email (fetched server-side)")
    blob = html + json.dumps(b.cookies()) + json.dumps(r1) + json.dumps(r2)
    check(raw_code not in blob, "raw activation code never appears in HTML, cookies or chat responses")
    check(not [n for n in b.cookies() if n.startswith("shop_activation_")], "no activation cookies are set")
    other = Browser(base)
    other.req("GET", "/")
    _, _, html = other.req("GET", "/account/activation")
    check("no account setup in progress" in html.lower(), "a different browser sees no pending setup")
    csrf = other.csrf("/account/activation")
    code, _, _ = other.req("POST", "/account/activation", form={"csrf": csrf, "password": PW})
    check(code != 302 and pg.query("SELECT status FROM customers WHERE email='chat.int@example.com'")[0][0] == "pending_activation",
          "a different browser cannot activate it")
    csrf = b.csrf("/account/activation")
    code, hdrs, _ = b.req("POST", "/account/activation", form={"csrf": csrf, "password": PW})
    check(code == 302 and hdrs.get("Location", "").endswith("/account/profile") and b.cookie("shop_account-session"),
          "same browser activates -> logged in")
    check(pg.query("SELECT status FROM customers WHERE email='chat.int@example.com'")[0][0] == "active", "Postgres account now active")
    check(not rds.keys("activation_handoff:*"), "handoff removed from Redis after activation")
    code, _, _ = b.req("POST", "/account/activation", form={"csrf": b.csrf("/account/activation"), "password": "Another1Passw0rd"})
    check(code != 302, "activation cannot be replayed")
    code, _, html = b.req("GET", "/account/profile")
    check(code == 200 and 'value="Demo"' in html and 'value="Shopper"' in html, "activated account profile renders (first/last name split)")

    # 3. Postgres outage: logged-in customer keeps identity, guest shopping and chat keep working
    pg.stop()
    try:
        code, _, html = a.req("GET", "/account/profile")
        check(code == 200 and "temporarily unavailable" in html and a.cookie("shop_account-session"),
              "outage: profile shows unavailable, auth cookie kept, no redirect to login")
        code, r = a.chat("show me mugs", "browse")
        check(code == 200 and "Mug" in r.get("message", ""), "outage: logged-in customer can still chat/browse")
        g = Browser(base)
        g.req("GET", "/")
        code, r = g.chat("show me mugs", "browse")
        check(code == 200 and "Mug" in r.get("message", ""), "outage: anonymous shopper can still chat/browse")
        code, _, home = g.req("GET", "/_healthz")
        check(code == 200, "outage: frontend healthy")
        code, _, html = Browser(base).req("GET", "/account/create")
        check(code == 200, "outage: signup page still renders")
        c2 = Browser(base)
        csrf = c2.csrf("/account/create")
        code, _, html = c2.req("POST", "/account/create", form={
            "csrf": csrf, "first_name": "Late", "last_name": "Shopper", "email": "late@example.com", "password": PW, "street_address": "1 Main St",
            "city": "Austin", "state": "TX", "zip_code": "73301", "country": "United States", "preferred_currency": ""})
        check(code == 200 and "temporarily unavailable" in html, "outage: signup shows a clear unavailable message (no 500)")
    finally:
        pg.start()
    # 4. Recovery without restarting any process or logging in again
    ok = False
    for _ in range(20):
        code, _, html = a.req("GET", "/account/profile")
        if code == 200 and 'value="Web"' in html and 'value="Shopper"' in html:
            ok = True
            break
        time.sleep(0.5)
    check(ok, "recovery: same auth cookie sees the profile again, no restart, no re-login")
    code, _, _ = Browser(base).req("POST", "/login", form={"csrf": "", "email": "x", "password": "y"})
    c3 = Browser(base)
    csrf = c3.csrf("/login")
    code, _, _ = c3.req("POST", "/login", form={"csrf": csrf, "email": "chat.int@example.com", "password": PW})
    check(code == 302, "recovery: activated chat account can log in")


if __name__ == "__main__":
    if not shutil.which("redis-server"):
        print("SKIP: redis-server not on PATH")
        sys.exit(2)
    main()
