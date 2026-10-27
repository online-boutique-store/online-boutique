"""Migration chain tests: dependency order (payment_methods 0004 before orders 0005), legacy upgrade,
idempotency, checksum detection, constraints. Real PostgreSQL (private cluster, see pgtest.py)."""
import os
import shutil
import sys
import tempfile
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.dirname(HERE)]

import psycopg  # noqa: E402

import accounts_db as adb  # noqa: E402
import pgtest  # noqa: E402

FAILS = []
MIG = adb.MIGRATIONS_DIR


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    if not cond:
        FAILS.append(name)


def fresh_db():
    name = "t" + uuid.uuid4().hex[:10]
    pgtest.cluster().create_db(name)
    return name, pgtest.cluster().dsn(name)


def tables(name):
    return {r[0] for r in pgtest.cluster().query(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='public'", db=name)}


def copy_migrations(versions, mutate=None):
    d = tempfile.mkdtemp(prefix="mig")
    for fn in sorted(os.listdir(MIG)):
        if fn.split("_", 1)[0] in versions:
            shutil.copy(os.path.join(MIG, fn), d)
    return d


# --- pure planner ------------------------------------------------------------------------------
def test_planner():
    f = lambda v, req=(), cs="c": (v, cs + v, "SELECT 1", tuple(req))   # noqa: E731
    files = [f("0001"), f("0002", ["0001"]), f("0003", ["0002"]), f("0004", ["0002"]), f("0005", ["0004"])]
    check("planner: fresh install applies all in order", [p[0] for p in adb.plan_migrations(files, {})] == ["0001", "0002", "0003", "0004", "0005"])
    applied = {v[0]: v[1] for v in files[:3]}
    check("planner: upgrade applies only the suffix", [p[0] for p in adb.plan_migrations(files, applied)] == ["0004", "0005"])
    try:
        adb.plan_migrations([f("0001"), f("0002", ["0001"]), f("0005", ["0004"])], {})
        check("planner: 0005 without 0004 is refused", False, "no error")
    except adb.MigrationOrderError as e:
        check("planner: 0005 without 0004 is refused", "requires 0004" in str(e), e)
    try:
        adb.plan_migrations([f("0001"), f("0002", ["0001"]), f("0003", ["0002"])], {"0001": "c0001", "0002": "c0002", "0003": "tampered"})
        check("planner: checksum mismatch detected", False, "no error")
    except adb.MigrationOrderError:
        check("planner: checksum mismatch detected", False, "wrong error")
    except adb.ChecksumMismatch:
        check("planner: checksum mismatch detected", True)
    try:
        adb.plan_migrations([f("0001"), f("0002", ["0001"]), f("0003", ["0002"])], {"0001": "c0001", "0003": "c0003"})
        check("planner: skipped (older than applied) refused", False, "no error")
    except adb.MigrationOrderError:
        check("planner: skipped (older than applied) refused", True)
    declared = {fn.split("_", 1)[0]: req for fn, req in ((v[0], v[3]) for v in adb.migration_files())}
    check("every real migration after 0001 declares requires", all(declared[v] for v in declared if v != "0001"), declared)
    check("0005 requires 0004; 0004 requires 0002", declared["0005"] == ("0004",) and declared["0004"] == ("0002",), declared)
    for _, _, text, _ in adb.migration_files():
        up = "\n".join(l for l in text.upper().splitlines() if not l.strip().startswith("--"))
        check("migration is transactional (no BEGIN/COMMIT/CONCURRENTLY/VACUUM)",
              not any(w in up for w in ("BEGIN;", "COMMIT;", "CONCURRENTLY", "VACUUM")))


# --- real database ------------------------------------------------------------------------------
def test_fresh_chain():
    name, dsn = fresh_db()
    out = adb.run_migrations(dsn)
    check("fresh DB applies the whole chain", out[0] == "applied" and out[2] == "0005", out)
    t = tables(name)
    check("all tables exist", {"customers", "addresses", "customer_addresses", "payment_methods", "orders", "order_items",
                               "orders_reconciliation", "activation_claims", "login_events", "schema_migrations"} <= t, t)
    fk = pgtest.cluster().query(
        "SELECT ccu.table_name FROM information_schema.table_constraints tc JOIN information_schema.constraint_column_usage ccu "
        "ON ccu.constraint_name = tc.constraint_name WHERE tc.table_name='orders' AND tc.constraint_type='FOREIGN KEY'", db=name)
    check("orders references payment_methods and customers", {r[0] for r in fk} >= {"payment_methods", "customers"}, fk)
    check("re-run is idempotent", adb.run_migrations(dsn)[0] == "up_to_date")
    # checksum drift is detected
    d = copy_migrations({"0001", "0002", "0003", "0004", "0005"})
    p = os.path.join(d, "0003_customer_addresses.sql")
    open(p, "a").write("\n-- edited\n")
    try:
        adb.run_migrations(dsn, d)
        check("edited migration is detected", False, "no error")
    except adb.ChecksumMismatch:
        check("edited migration is detected", True)
    return name, dsn


def test_0005_without_0004_fails_and_leaves_nothing():
    name, dsn = fresh_db()
    d = copy_migrations({"0001", "0002", "0003", "0005"})
    try:
        adb.run_migrations(dsn, d)
        check("0005 without 0004 fails", False, "no error")
    except adb.MigrationOrderError:
        check("0005 without 0004 fails", True)
    t = tables(name)
    check("nothing applied by the failed attempt", "orders" not in t and "customers" not in t, t)
    # and the full directory then works from that same database
    out = adb.run_migrations(dsn)
    check("full chain then applies cleanly", out[0] == "applied" and out[2] == "0005", out)
    # applying 0005 SQL text directly without payment_methods fails at the database level too
    name2, dsn2 = fresh_db()
    d2 = copy_migrations({"0001", "0002", "0003"})
    adb.run_migrations(dsn2, d2)
    try:
        with psycopg.connect(dsn2) as c:
            c.execute(open(os.path.join(MIG, "0005_orders.sql")).read())
        check("0005 SQL without payment_methods errors in Postgres", False, "no error")
    except psycopg.errors.UndefinedTable:
        check("0005 SQL without payment_methods errors in Postgres", True)


def test_legacy_upgrade():
    name, dsn = fresh_db()
    adb.run_migrations(dsn, copy_migrations({"0001"}))
    c = pgtest.cluster()
    ids = {k: str(uuid.uuid4()) for k in ("ok", "single", "junkstate", "badzip", "nonus", "po", "pending")}
    rows = [("ok", "ok@example.com", "Ada Lovelace", "active", "web"),
            ("single", "single@example.com", "Cher", "active", "assistant"),
            ("junkstate", "junk@example.com", "Jun K State", "active", "web"),
            ("badzip", "badzip@example.com", "Bad Zip", "active", "web"),
            ("nonus", "nonus@example.com", "Non Us", "active", "web"),
            ("po", "po@example.com", "Po Box", "active", "web"),
            ("pending", "pending@example.com", "Pen Ding", "pending_activation", "assistant")]
    for k, email, nm, status, via in rows:
        c.query("INSERT INTO customers (id, email, name, status, created_via, password_hash) VALUES (%s,%s,%s,%s,%s,%s)",
                (ids[k], email, nm, status, via, "x" if status == "active" else None), db=name)
    addr = {"ok": ("12 Main St", "Austin", "tx", "73301", "United States"),
            "single": ("5 Oak Rd", "Reno", "NV", "89501", "US"),
            "junkstate": ("1 A St", "Nowhere", "Texas", "73301", "US"),
            "badzip": ("2 B St", "Austin", "TX", "7330", "US"),
            "nonus": ("3 C St", "Toronto", "ON", "M5V2T6", "Canada"),
            "po": ("PO Box 9", "Austin", "TX", "73301-1234", "USA")}
    for k, (s, ci, st, z, co) in addr.items():
        c.query("INSERT INTO addresses (customer_id, street_address, city, state, zip_code, country) VALUES (%s,%s,%s,%s,%s,%s)",
                (ids[k], s, ci, st, z, co), db=name)
    out = adb.run_migrations(dsn)
    check("legacy DB upgrades 0001 -> 0005", out[0] == "applied" and out[2] == "0005", out)
    prof = {r[0]: r for r in c.query("SELECT email, first_name, last_name, email_display, phone_e164, profile_version FROM customers", db=name)}
    check("legacy rows become profile_version 1", all(r[5] == 1 for r in prof.values()))
    check("name split into first/last", prof["ok@example.com"][1:3] == ("Ada", "Lovelace") and prof["junk@example.com"][1:3] == ("Jun", "K State"), prof)
    check("single-token name keeps first, last NULL", prof["single@example.com"][1:3] == ("Cher", None), prof["single@example.com"])
    check("email_display backfilled from email, phone stays NULL", prof["ok@example.com"][3] == "ok@example.com" and prof["ok@example.com"][4] is None)
    saved = {r[0]: r for r in c.query("SELECT c.email, ca.line1, ca.state, ca.zip, ca.is_default, ca.is_po_box FROM customer_addresses ca JOIN customers c ON c.id = ca.customer_id", db=name)}
    check("valid legacy addresses copied with normalized state", saved["ok@example.com"][1:5] == ("12 Main St", "TX", "73301", True), saved.get("ok@example.com"))
    check("junk state / bad ZIP / non-US stay legacy-only", not ({"junk@example.com", "badzip@example.com", "nonus@example.com"} & set(saved)), set(saved))
    check("PO Box flagged and ZIP+4 kept", saved["po@example.com"][5] is True and saved["po@example.com"][3] == "73301-1234", saved.get("po@example.com"))
    legacy = c.query("SELECT count(*) FROM addresses", db=name)[0][0]
    check("legacy addresses table untouched", legacy == 6, legacy)
    # old-code compatibility: legacy login query still works against the new schema
    row = c.query("SELECT password_hash, status FROM customers WHERE email='ok@example.com'", db=name)[0]
    check("old readers still see password_hash/status", row == ("x", "active"), row)
    return name


def test_constraints():
    name, dsn = fresh_db()
    adb.run_migrations(dsn)
    c = pgtest.cluster()

    def ins(sql, params=()):
        try:
            c.query(sql, params, db=name)
            return None
        except psycopg.errors.CheckViolation as e:
            return "check"
        except psycopg.errors.ForeignKeyViolation:
            return "fk"
        except psycopg.errors.UniqueViolation:
            return "unique"
        except psycopg.errors.DataError:
            return "data"

    cid = str(uuid.uuid4())
    base = "INSERT INTO customers (id, email, name, status, created_via, password_hash, profile_version, first_name, last_name, phone_e164, email_display) VALUES (%s,%s,%s,'active','web','x',%s,%s,%s,%s,%s)"
    check("v1 customer without phone/names is accepted", ins(base, (cid, "v1@example.com", "N", 1, None, None, None, None)) is None)
    check("v2 customer missing phone is rejected", ins(base, (str(uuid.uuid4()), "v2a@example.com", "N", 2, "A", "B", None, "v2a@example.com")) == "check")
    check("v2 customer missing last name is rejected", ins(base, (str(uuid.uuid4()), "v2b@example.com", "N", 2, "A", None, "+14155552671", "v2b@example.com")) == "check")
    check("complete v2 customer accepted", ins(base, (str(uuid.uuid4()), "v2c@example.com", "N", 2, "A", "B", "+14155552671", "v2c@example.com")) is None)
    check("bad phone format rejected", ins(base, (str(uuid.uuid4()), "p@example.com", "N", 1, "A", "B", "4155552671", None)) == "check")
    check("area code starting 1 rejected", ins(base, (str(uuid.uuid4()), "p2@example.com", "N", 1, "A", "B", "+11155552671", None)) == "check")
    check("profile_version 3 rejected", ins(base, (str(uuid.uuid4()), "p3@example.com", "N", 3, "A", "B", "+14155552671", "p3@example.com")) == "check")
    check("mixed-case email rejected by the existing key check", ins("INSERT INTO customers (email,name,status,created_via,password_hash) VALUES ('Mixed@Example.com','N','active','web','x')") == "check")
    addr = "INSERT INTO customer_addresses (customer_id, line1, city, state, zip, is_default) VALUES (%s,%s,%s,%s,%s,%s)"
    check("valid saved address accepted", ins(addr, (cid, "1 Main", "Austin", "TX", "73301", True)) is None)
    check("second default for the customer rejected", ins(addr, (cid, "2 Main", "Austin", "TX", "73301", True)) == "unique")
    check("lowercase state rejected", ins(addr, (cid, "3 Main", "Austin", "tx", "73301", False)) == "check")
    check("bad ZIP rejected", ins(addr, (cid, "3 Main", "Austin", "TX", "7330", False)) == "check")
    check("non-US country rejected", ins("INSERT INTO customer_addresses (customer_id, line1, city, state, zip, country) VALUES (%s,'1 A','B','TX','73301','CA')", (cid,)) == "check")
    pm = str(uuid.uuid4())
    check("payment method accepted", ins("INSERT INTO payment_methods (id, customer_id, provider_token, catalog_id, brand, last4, exp_month, exp_year, is_default) "
                                         "VALUES (%s,%s,'tok_demo_a','demo_visa_1111','visa','1111',12,2040,true)", (pm, cid)) is None)
    check("PAN-length last4 rejected", ins("INSERT INTO payment_methods (customer_id, provider_token, catalog_id, brand, last4, exp_month, exp_year) "
                                           "VALUES (%s,'tok_demo_b','x','visa','4111111111111111',12,2040)", (cid,)) == "data")
    check("unknown brand rejected", ins("INSERT INTO payment_methods (customer_id, provider_token, catalog_id, brand, last4, exp_month, exp_year) "
                                        "VALUES (%s,'tok_demo_c','x','jcb','1111',12,2040)", (cid,)) == "check")
    check("duplicate provider token rejected", ins("INSERT INTO payment_methods (customer_id, provider_token, catalog_id, brand, last4, exp_month, exp_year) "
                                                   "VALUES (%s,'tok_demo_a','x','visa','1111',12,2040)", (cid,)) == "unique")
    oid = str(uuid.uuid4())
    order = ("INSERT INTO orders (order_id, customer_id, email_hash, channel, record_state, placed_at, currency, total_units, total_nanos, payment_method_id) "
             "VALUES (%s,%s,'h','web',%s,%s,'USD',10,0,%s)")
    check("order with real payment method accepted", ins(order, (oid, cid, "confirmed", "2026-01-01", pm)) is None)
    check("order with unknown payment method rejected", ins(order, (str(uuid.uuid4()), cid, "confirmed", "2026-01-01", str(uuid.uuid4()))) == "fk")
    check("guest order (NULL customer, NULL payment method) accepted", ins(order, (str(uuid.uuid4()), None, "confirmed", "2026-01-01", None)) is None)
    check("confirmed order requires placed_at", ins(order, (str(uuid.uuid4()), cid, "confirmed", None, None)) == "check")
    check("intent order without placed_at accepted", ins(order, (str(uuid.uuid4()), cid, "intent", None, None)) is None)
    check("order item quantity must be positive", ins("INSERT INTO order_items (order_id, line_no, product_id, name, quantity) VALUES (%s,1,'p','n',0)", (oid,)) == "check")
    c.query("DELETE FROM payment_methods WHERE id=%s", (pm,), db=name)
    left = c.query("SELECT payment_method_id FROM orders WHERE order_id=%s", (oid,), db=name)
    check("deleting a payment method nulls the order reference; order survives", left == [(None,)], left)
    c.query("DELETE FROM customers WHERE id=%s", (cid,), db=name)
    left = c.query("SELECT customer_id FROM orders WHERE order_id=%s", (oid,), db=name)
    check("deleting a customer keeps the order (customer_id NULL)", left == [(None,)], left)


if __name__ == "__main__":
    test_planner()
    test_fresh_chain()
    test_0005_without_0004_fails_and_leaves_nothing()
    test_legacy_upgrade()
    test_constraints()
    print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'}")
    sys.exit(1 if FAILS else 0)
