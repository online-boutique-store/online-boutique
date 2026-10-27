"""Run: python tests/test_accounts_pg.py   (from src/shoppingassistantservice)

Real PostgreSQL (bundled cluster, see pgtest.py) + in-memory Redis. Covers lifecycle/persistence,
concurrency, server-side activation, cross-store failures, migration locking, schema readiness
after outages AND database replacement, identity during outages, actual exported spans and
sensitive-data filtering.
"""
import json
import logging
import os
import re
import sys
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor

os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"] = "true"
os.environ["ACCOUNT_API_SECRET"] = "internal-test-secret"
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(HERE)]

from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

exporter = InMemorySpanExporter()
tp = TracerProvider()
tp.add_span_processor(SimpleSpanProcessor(exporter))
trace.set_tracer_provider(tp)
reader = InMemoryMetricReader()
metrics.set_meter_provider(MeterProvider(metric_readers=[reader]))

import pgtest  # noqa: E402
import accounts  # noqa: E402
import accounts_db  # noqa: E402
import checkout  # noqa: E402
import config  # noqa: E402
import shoppingassistantservice as svc  # noqa: E402  (initialises telemetry + psycopg instrumentation)

records = []


class Capture(logging.Handler):
    def emit(self, record):
        records.append(record)


logging.getLogger("assistant").addHandler(Capture())

ADDR = dict(street_address="1 Main St", city="Austin", state="TX", zip_code="73301", country="United States")
PW = "Sup3rPassw0rd!"
SECRET = {"X-Account-Api-Secret": "internal-test-secret"}
PG = pgtest.cluster()
REPO = pgtest.new_repo()
tests = []


def test(fn):
    tests.append(fn)
    return fn


def fresh(redis=None):
    global REPO
    if REPO is not None:
        REPO.close()
    REPO = pgtest.new_repo()
    pgtest.reset_data(REPO)
    store = accounts.AccountStore(redis or pgtest.FakeRedis(), repo=REPO)
    accounts.store = store
    return store


def profile(email="demo@example.com", name="Demo Shopper"):
    return {"name": name, "email": email, "address": dict(ADDR), "preferred_currency": "USD"}


def rows(sql, params=()):
    return PG.query(sql, params)


def unavailable_raises(fn, *a, **k):
    try:
        fn(*a, **k)
    except accounts.AccountStoreUnavailable as e:
        return e.reason
    return None


def eventually(fn, tries=6):
    last = None
    for _ in range(tries):
        try:
            return fn()
        except accounts.AccountStoreUnavailable as e:
            last = e
    raise AssertionError(f"still unavailable: {last}")


# 1 ---------------------------------------------------------------------------------------------
@test
def test_lifecycle_and_persistence_across_postgres_restart():
    st = fresh()
    ok, acct = st.register(profile(), PW)
    assert ok and acct["id"]
    assert st.authenticate("DEMO@example.com", PW) and not st.authenticate("demo@example.com", "wrong-pass1")
    st.update("demo@example.com", name="Renamed Shopper", preferred_currency="EUR",
              address=dict(ADDR, city="Dallas"))
    PG.stop()
    PG.start()
    got = eventually(lambda: st.get("demo@example.com"))
    assert got["name"] == "Renamed Shopper" and got["preferred_currency"] == "EUR" and got["address"]["city"] == "Dallas"
    assert REPO.schema.state in ("ready", "stale")
    assert eventually(lambda: st.authenticate("demo@example.com", PW))
    token = st.create_session(got["email"], got["id"], got["name"])
    assert st.session_account(token)["email"] == "demo@example.com"
    st.delete_session(token)
    assert st.session_account(token) is None


# 2 ---------------------------------------------------------------------------------------------
@test
def test_duplicates_and_concurrency():
    st = fresh()
    assert st.register(profile(), PW)[0]
    assert not st.register(profile("DEMO@Example.com"), PW)[0]          # case-insensitive duplicate
    assert rows("SELECT count(*) FROM customers")[0][0] == 1
    st = fresh()
    with ThreadPoolExecutor(10) as ex:
        res = list(ex.map(lambda i: st.register(profile("race@example.com"), PW)[0], range(20)))
    assert res.count(True) == 1 and res.count(False) == 19, res
    st = fresh()
    outs = [st.create_pending("S1", "Demo", "dup@example.com", dict(ADDR))["outcome"] for _ in range(2)]
    assert outs == ["created", "resumed"], outs                           # double-submit is idempotent
    assert rows("SELECT count(*), max(rotations) FROM customers c JOIN activation_claims a ON a.customer_id=c.id") == [(1, 1)]
    with ThreadPoolExecutor(6) as ex:
        outs = list(ex.map(lambda i: st.create_pending("S2", "Demo", "same@example.com", dict(ADDR))["outcome"], range(6)))
    assert rows("SELECT count(*) FROM customers WHERE email='same@example.com'")[0][0] == 1
    assert outs.count("created") == 1, outs


# 3 ---------------------------------------------------------------------------------------------
@test
def test_server_side_activation_flow_over_http():
    redis = pgtest.FakeRedis()
    st = fresh(redis)
    res = st.create_pending("browser-1", "Demo Shopper", "chat@example.com", dict(ADDR))
    assert res["outcome"] == "created" and res["handoff"]
    key = [k for k in redis.d if k.startswith("activation_handoff:")][0]
    assert 0 < redis.ttl[key] <= 900
    raw_code = json.loads(redis.d[key])["raw_code"]
    assert rows("SELECT code_hash FROM activation_claims")[0][0] != raw_code
    c = svc.app.test_client()
    r = c.get("/account/activation", headers={**SECRET, "X-Session-ID": "browser-1"})
    assert r.status_code == 200 and r.get_json() == {"pending": True, "email": "chat@example.com"}
    assert raw_code not in r.get_data(as_text=True)
    other = c.get("/account/activation", headers={**SECRET, "X-Session-ID": "browser-2"})
    assert other.status_code == 404
    bad = c.post("/account/claim", headers=SECRET, json={"session_id": "browser-2", "password": PW})
    assert bad.status_code == 400 and bad.get_json() == {"error": "activation_failed"}
    ok = c.post("/account/claim", headers=SECRET, json={"session_id": "browser-1", "password": PW})
    assert ok.status_code == 200 and ok.get_json()["session_token"], ok.get_json()
    assert raw_code not in ok.get_data(as_text=True) and key not in redis.d
    again = c.post("/account/claim", headers=SECRET, json={"session_id": "browser-1", "password": PW})
    assert again.status_code == 400
    login = c.post("/account/login", headers=SECRET, json={"email": "chat@example.com", "password": PW})
    assert login.status_code == 200
    assert rows("SELECT status FROM customers")[0][0] == "active"


# 4 ---------------------------------------------------------------------------------------------
@test
def test_cross_store_partial_failures_and_takeover_protection():
    # Redis fails AFTER the Postgres commit: account stays valid, handoff regenerates for the same session only.
    redis = pgtest.FakeRedis()
    st = fresh(redis)
    redis.fail_writes = True
    res = st.create_pending("S1", "Demo", "p@example.com", dict(ADDR))
    assert res["outcome"] == "created" and res["handoff"] is False
    assert rows("SELECT count(*) FROM customers")[0][0] == 1
    redis.fail_writes = False
    rot = rows("SELECT rotations FROM activation_claims")[0][0]
    assert st.activation_status("S-other") == {"pending": False}
    assert rows("SELECT rotations FROM activation_claims")[0][0] == rot        # other session did not rotate it
    assert st.activation_status("S1") == {"pending": True, "email": "p@example.com"}
    acct, token = st.claim("S1", PW)
    assert acct and token
    # Postgres fails mid-transaction: nothing persisted, nothing written to Redis.
    redis2 = pgtest.FakeRedis()
    st = fresh(redis2)
    original = accounts_db.PgAccountRepository._new_claim
    accounts_db.PgAccountRepository._new_claim = staticmethod(lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    try:
        try:
            st.create_pending("S1", "Demo", "q@example.com", dict(ADDR))
            raise AssertionError("expected failure")
        except RuntimeError:
            pass
    finally:
        accounts_db.PgAccountRepository._new_claim = staticmethod(original)
    assert rows("SELECT count(*) FROM customers")[0][0] == 0 and not [k for k in redis2.d if "handoff" in k]
    # Another session while the claim is live: no takeover, no rotation, no handoff.
    st = fresh()
    first = st.create_pending("S1", "Demo", "t@example.com", dict(ADDR))
    r2 = st.create_pending("S2", "Evil", "t@example.com", dict(ADDR))
    assert r2["outcome"] == "pending_elsewhere" and not r2["handoff"]
    assert rows("SELECT rotations FROM activation_claims")[0][0] == 0
    assert rows("SELECT name FROM customers")[0][0] == "Demo"
    assert not st.register(profile("t@example.com"), PW)[0]                    # web signup also refused
    # After the claim expires the abandoned account no longer blocks the email.
    PG.query("UPDATE activation_claims SET expires_at = now() - interval '1 minute'")
    r3 = st.create_pending("S2", "New Owner", "t@example.com", dict(ADDR))
    assert r3["outcome"] == "recreated_abandoned" and r3["customer_id"] != first["customer_id"]
    assert st.activation_status("S1") == {"pending": False}                    # old session cannot get a code
    PG.query("UPDATE activation_claims SET expires_at = now() - interval '1 minute'")
    assert st.register(profile("t@example.com", "Web Owner"), PW)[0]            # web signup reclaims it too
    # Session write fails after activation: account is active, caller is told to log in.
    redis3 = pgtest.FakeRedis()
    st = fresh(redis3)
    st.create_pending("S1", "Demo", "r@example.com", dict(ADDR))
    redis3.fail_writes = True
    acct, token = st.claim("S1", PW)
    redis3.fail_writes = False
    assert acct and token is None and st.authenticate("r@example.com", PW)
    # Pending accounts cannot log in.
    st = fresh()
    st.create_pending("S1", "Demo", "pend@example.com", dict(ADDR))
    assert st.authenticate("pend@example.com", PW) is None


@test
def test_concurrent_activation_and_reaper():
    st = fresh()
    st.create_pending("S1", "Demo", "c@example.com", dict(ADDR))
    with ThreadPoolExecutor(10) as ex:
        res = list(ex.map(lambda i: st.claim("S1", PW)[0], range(10)))
    assert len([r for r in res if r]) == 1, res
    st = fresh()
    st.create_pending("S1", "Demo", "d@example.com", dict(ADDR))
    out = {}

    def claim():
        out["claim"] = st.claim("S1", PW)[0]

    def regen():
        out["regen"] = REPO.regenerate_claim(accounts._hash_secret("S1"), accounts._hash_secret("rotated"))

    ts = [threading.Thread(target=claim), threading.Thread(target=regen)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert rows("SELECT count(*) FROM customers WHERE status='active'")[0][0] <= 1
    # reaper
    st = fresh()
    st.create_pending("A", "Old", "old@example.com", dict(ADDR))
    st.create_pending("B", "Live", "live@example.com", dict(ADDR))
    st.register(profile("active@example.com"), PW)
    PG.query("UPDATE activation_claims SET expires_at = now() - interval '2 hours' WHERE customer_id = "
             "(SELECT id FROM customers WHERE email='old@example.com')")
    with ThreadPoolExecutor(2) as ex:
        n = sum(ex.map(lambda i: REPO.reap(), range(2)))
    assert n == 1, n
    assert sorted(r[0] for r in rows("SELECT email FROM customers")) == ["active@example.com", "live@example.com"]


@test
def test_stale_handoff_after_successful_activation():
    redis = pgtest.FakeRedis()
    st = fresh(redis)
    st.create_pending("S1", "Demo", "stale@example.com", dict(ADDR))
    key = [k for k in redis.d if k.startswith("activation_handoff:")][0]
    redis.fail_writes = True                     # handoff delete AND session write fail after the PG commit
    acct, token = st.claim("S1", PW)
    redis.fail_writes = False
    assert acct and token is None and key in redis.d, "stale handoff should remain after the failed delete"
    assert rows("SELECT status FROM customers")[0][0] == "active"
    rot = rows("SELECT rotations, used_at IS NOT NULL FROM activation_claims")[0]
    assert st.activation_status("S1") == {"pending": False}       # validated against Postgres, not trusted
    assert key not in redis.d, "stale handoff must be removed once Postgres says the claim is used"
    assert rows("SELECT rotations, used_at IS NOT NULL FROM activation_claims")[0] == rot   # no regeneration
    assert st.claim("S1", "Other1Passw0rd") == (None, None)       # cannot re-activate / reset the password
    assert st.authenticate("stale@example.com", PW) and not st.authenticate("stale@example.com", "Other1Passw0rd")
    c = svc.app.test_client()
    assert c.get("/account/activation", headers={**SECRET, "X-Session-ID": "S1"}).status_code == 404


# 5 ---------------------------------------------------------------------------------------------
@test
def test_migration_locking_rollback_and_checksums():
    PG.drop_db("mig")
    PG.create_db("mig")
    dsn = PG.dsn("mig")
    import psycopg
    holder = psycopg.connect(dsn, autocommit=False)
    holder.execute("SELECT pg_try_advisory_xact_lock(hashtext(%s))", (accounts_db.MIGRATION_LOCK_KEY,))
    assert accounts_db.run_migrations(dsn)[0] == "locked_elsewhere"
    holder.commit()
    holder.close()
    assert accounts_db.run_migrations(dsn)[0] == "applied"
    assert accounts_db.run_migrations(dsn)[0] == "up_to_date"
    # concurrent migrators on a fresh db: exactly one applies
    PG.replace_db("mig")
    with ThreadPoolExecutor(4) as ex:
        outs = list(ex.map(lambda i: accounts_db.run_migrations(dsn)[0], range(4)))
    assert outs.count("applied") == 1 and set(outs) <= {"applied", "locked_elsewhere", "up_to_date"}, outs
    # no advisory lock leaks anywhere
    assert PG.query("SELECT count(*) FROM pg_locks WHERE locktype = 'advisory'", db="postgres")[0][0] == 0
    # failure mid-file rolls everything back, retry succeeds
    real = accounts_db.migration_files
    good = real()
    bad = good + [("0002", "x" * 64, "CREATE TABLE half_done (id int);\nSELECT * FROM table_that_does_not_exist;")]
    accounts_db.migration_files = lambda: bad
    try:
        try:
            accounts_db.run_migrations(dsn)
            raise AssertionError("expected failure")
        except psycopg.errors.UndefinedTable:
            pass
        assert PG.query("SELECT count(*) FROM schema_migrations WHERE version='0002'", db="mig")[0][0] == 0
        assert PG.query("SELECT to_regclass('half_done')", db="mig")[0][0] is None
        accounts_db.migration_files = lambda: good + [("0002", "y" * 64, "CREATE TABLE half_done (id int);")]
        assert accounts_db.run_migrations(dsn)[0] == "applied"
    finally:
        accounts_db.migration_files = real
    # checksum mismatch keeps operations gated
    PG.query("UPDATE schema_migrations SET checksum = 'tampered' WHERE version = '0001'", db="mig")
    repo = accounts_db.PgAccountRepository(dsn=dsn, retry_debounce_s=0)
    try:
        repo.schema.attempt()
        assert repo.schema.state == "failed"
        assert unavailable_raises(repo.get_customer, "x@example.com") == "schema_mismatch"
    finally:
        repo.close()
    for name in os.listdir(accounts_db.MIGRATIONS_DIR):
        text = "\n".join(l for l in open(os.path.join(accounts_db.MIGRATIONS_DIR, name)).read().splitlines()
                         if not l.strip().startswith("--")).upper()
        for bad_stmt in ("CONCURRENTLY", "VACUUM", "BEGIN;", "COMMIT;"):
            assert bad_stmt not in text, (name, bad_stmt)


# 6 ---------------------------------------------------------------------------------------------
@test
def test_schema_readiness_outage_replacement_and_reinit():
    # start while Postgres is DOWN: gated, no table SQL issued, recovers in-process
    PG.stop()
    exporter.clear()
    repo = pgtest.new_repo(retry_debounce_s=0, ready=False)
    st = accounts.AccountStore(pgtest.FakeRedis(), repo=repo)
    assert unavailable_raises(st.get, "a@example.com") in ("unreachable", "schema_not_ready")
    assert not [s for s in exporter.get_finished_spans() if "customers" in str(s.attributes.get("db.statement", ""))]
    PG.start()
    repo.start()
    assert pgtest.wait_ready.__call__(repo, 20)
    assert st.get("a@example.com") is None                                    # same process, no restart
    first_instance = repo.schema.instance_id
    assert first_instance
    ok, _ = st.register(profile("keep@example.com"), PW)
    # temporary outage on the SAME instance: revalidates, instance unchanged, no re-migration
    PG.stop()
    assert unavailable_raises(st.get, "keep@example.com") in ("unreachable", "timeout", "schema_not_ready")
    PG.start()
    assert eventually(lambda: st.get("keep@example.com"))["email"] == "keep@example.com"
    assert repo.schema.instance_id == first_instance
    # database REPLACED by an empty one behind the same DSN
    PG.replace_db("accounts")
    assert eventually(lambda: st.get("keep@example.com")) is None
    assert repo.schema.state == "ready" and repo.schema.instance_id != first_instance
    assert eventually(lambda: st.register(profile("new@example.com"), PW))[0]
    # tables present but a different instance id (restored from elsewhere): detected and revalidated
    before = repo.schema.instance_id
    PG.query("UPDATE accounts_meta SET instance_id = gen_random_uuid()")
    repo.schema.mark_stale("test")
    assert st.get("new@example.com")["email"] == "new@example.com"
    assert repo.schema.instance_id != before and repo.schema.state == "ready"
    repo.close()


# 7 ---------------------------------------------------------------------------------------------
@test
def test_identity_preserved_and_guest_checkout_during_outage():
    redis = pgtest.FakeRedis()
    st = fresh(redis)
    c = svc.app.test_client()
    reg = c.post("/account/register", headers=SECRET, json={**profile("id@example.com"), "first_name": "Demo", "last_name": "Shopper", "password": PW,
                                                           "confirm_password": PW, **ADDR})
    assert reg.status_code == 201, reg.get_json()
    token = reg.get_json()["session_token"]
    auth = {**SECRET, "X-Account-Session": token}
    PG.stop()
    try:
        ctx = c.get("/account/context", headers=auth).get_json()["account"]
        assert ctx["email"] == "id@example.com" and ctx["name"] == "Demo Shopper"        # identity from Redis
        prof = c.get("/account/profile", headers=auth)
        assert prof.status_code == 503 and prof.get_json()["error"] == "account_store_unavailable"
        assert c.post("/account/login", headers=SECRET, json={"email": "id@example.com", "password": PW}).status_code == 503
        assert c.get("/healthz").status_code == 200
        # checkout for the logged-in customer: unavailable, NOT silently a guest
        checkout.backends.cart_contents = lambda uid: ([{"product_id": "P1", "name": "Mug", "quantity": 1, "unit_price_usd": 8.0}], 8.0)
        checkout.backends.place_order = lambda uid, cur, email, addr, card, **kw: {"order_id": "ord-1"}

        class S:
            id, turns = "sess-1", 0
            customer_account = dict(st.session_account(token))
            checkout = checkout.CheckoutState()

        s = S()
        s.checkout.session_id = "sess-1"
        checkout.start_checkout(s)
        r = checkout.lookup_account(s, "id@example.com")
        assert r["status"] == "account_unavailable" and "error" not in r
        assert s.checkout.customer_type == "returning" and s.checkout.email == "id@example.com"
        # explicit guest choice works end-to-end with the database down
        assert checkout.continue_as_guest(s, "id@example.com", "Demo")["status"] == "guest"
        assert s.checkout.customer_type == "guest"
        checkout.set_shipping_address(s, **ADDR)
        checkout.set_payment(s, "pm_demo_visa")
        checkout.set_currency(s, "USD")
        checkout.review_order(s)
        s.turns = 1
        checkout.confirm_order(s)
        assert checkout.place_order(s).get("order_id") == "ord-1"
        # anonymous chat-account creation degrades with the guest hint
        anon = type("A", (), {"id": "anon", "turns": 0, "checkout": checkout.CheckoutState()})()
        anon.checkout.session_id = "anon"
        checkout.start_checkout(anon)
        err = checkout.create_account(anon, first_name="X", last_name="Y", email="x@example.com", **ADDR)
        assert "error" in err and "guest" in err["hint"]
        # logout only needs Redis (a second session, so the first one can prove recovery below)
        other = st.create_session("id@example.com", ctx.get("customer_id", "x"), "Demo Shopper")
        assert c.post("/account/logout", headers={**SECRET, "X-Account-Session": other}).status_code == 200
        assert st.session_account(other) is None
    finally:
        PG.start()
    # recovery: same Redis session, no re-login, Postgres-backed endpoints work again
    prof = eventually(lambda: (lambda r: r if r.status_code == 200 else (_ for _ in ()).throw(
        accounts.AccountStoreUnavailable("unreachable")))(c.get("/account/profile", headers=auth)))
    assert prof.get_json()["account"]["email"] == "id@example.com"
    assert c.post("/account/login", headers=SECRET, json={"email": "id@example.com", "password": PW}).status_code == 200
    s2 = type("S2", (), {"id": "sess-2", "turns": 0, "customer_account": dict(st.session_account(token) or ctx),
                         "checkout": checkout.CheckoutState()})()
    s2.checkout.session_id = "sess-2"
    checkout.start_checkout(s2)
    r = checkout.lookup_account(s2, "id@example.com")
    assert r.get("found") is True and s2.checkout.customer_type == "returning"


# 8 + 9 ----------------------------------------------------------------------------------------
@test
def test_exported_spans_and_sensitive_data_filtering():
    redis = pgtest.FakeRedis()
    st = fresh(redis)
    exporter.clear()
    records.clear()
    tracer = trace.get_tracer("test")
    with tracer.start_as_current_span("account.create") as parent:
        res = st.create_pending("browser-sec", "Demo Shopper", "span@example.com", dict(ADDR))
    assert res["handoff"]
    raw_code = json.loads(redis.d[[k for k in redis.d if "handoff" in k][0]])["raw_code"]
    acct, token = st.claim("browser-sec", PW)
    assert acct and token
    try:
        with REPO._get_pool().connection() as conn, tracer.start_as_current_span("dup"):
            conn.execute("INSERT INTO customers (email, name, status, created_via, password_hash) "
                         "VALUES (%s, 'x', 'active', 'web', 'h')", ("span@example.com",))
    except Exception:  # noqa: BLE001 - unique violation expected
        pass
    spans = exporter.get_finished_spans()
    by_id = {s.context.span_id: s for s in spans}
    biz = [s for s in spans if s.name == "accounts.db.claim.create"][0]
    assert biz.parent.span_id == parent.get_span_context().span_id
    assert len({s.context.trace_id for s in spans if s.name in ("account.create", "accounts.db.claim.create")}) == 1
    clients = [s for s in spans if s.kind == trace.SpanKind.CLIENT and s.attributes.get("db.system") == "postgresql"]
    kids = [s for s in clients if s.parent and s.parent.span_id == biz.context.span_id]
    assert kids, "psycopg CLIENT spans must nest under the business span"
    statements = [str(s.attributes.get("db.statement") or "") for s in kids]
    ops = {stmt.split()[0].upper() for stmt in statements if stmt.split()}
    assert {"SELECT", "INSERT"} <= ops, ops
    for s in clients:
        assert s.attributes["db.name"] == "accounts" and s.end_time > s.start_time
        stmt = str(s.attributes.get("db.statement") or "")
        assert "span@example.com" not in stmt and "Austin" not in stmt, stmt
    assert any("%s" in str(s.attributes["db.statement"]) for s in kids), "SQL must be parameterized"
    errs = [s for s in clients if s.status.status_code == trace.StatusCode.ERROR]
    assert errs, "unique violation must set ERROR status on the db span"
    print("      observed psycopg span attributes:", sorted({k for s in clients for k in s.attributes}))
    # business span identity attrs present
    assert biz.attributes["app.customer.email"] == "span@example.com" and biz.attributes["app.customer.name"] == "Demo Shopper"
    assert biz.attributes["app.customer.id"] and biz.attributes["app.account.activation.outcome"] == "created"
    # sensitive-data scan across spans, events, logs and metric labels
    hashes = rows("SELECT password_hash FROM customers WHERE email='span@example.com'")[0][0]
    sess_hash = accounts._hash_secret("browser-sec")
    secrets_ = [PW, hashes, raw_code, accounts._hash_secret(raw_code), sess_hash, token, "1 Main St", "Austin", "73301",
                PG.dsn(), "pbkdf2_sha256"]
    blob = json.dumps([[dict(s.attributes), [(e.name, dict(e.attributes)) for e in s.events]] for s in spans], default=str)
    blob += json.dumps([{k: v for k, v in r.__dict__.items() if k not in ("args", "exc_info")} for r in records], default=str)
    data = reader.get_metrics_data()
    for rm in (data.resource_metrics if data else []):
        for sm in rm.scope_metrics:
            for m in sm.metrics:
                blob += json.dumps([dict(p.attributes) for p in m.data.data_points], default=str)
    for secret in secrets_:
        assert secret not in blob, f"sensitive value leaked into telemetry: {secret[:6]}..."
    assert "span@example.com" in blob and "Demo Shopper" in blob
    names = {m.name for rm in reader.get_metrics_data().resource_metrics for sm in rm.scope_metrics for m in sm.metrics}
    assert "app.account.db.duration" in names and "app.account.activation.events" in names


if __name__ == "__main__":
    failed = []
    only = sys.argv[1:]
    for t in [t for t in tests if not only or any(o in t.__name__ for o in only)]:
        try:
            t()
            print("PASS", t.__name__)
        except Exception:  # noqa: BLE001
            failed.append(t.__name__)
            print("FAIL", t.__name__)
            traceback.print_exc()
    print(f"\n{len(tests) - len(failed)}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
