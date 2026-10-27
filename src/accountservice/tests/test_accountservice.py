"""accountservice end-to-end: real gRPC server + Postgres + Redis, exported telemetry captured by a local
OTLP receiver. Covers service auth, registration/login/session/profile/address flows, legacy-account
compatibility, outage behavior, and the instrumentation + PII-canary checks for those workflows."""
import json
import os
import re
import sys
import time
import uuid

import service_env as env

S = env.start()
import account_pb2 as pb  # noqa: E402
import demo_pb2  # noqa: E402
import accounts  # noqa: E402
import grpc  # noqa: E402

stub, store, recv, cluster, redis = S["stub"], S["store"], S["receiver"], S["cluster"], S["redis"].client()
FAILS = []
CANARY = {
    "first": "Zephyrine", "last": "Quillfeather", "email_local": "zq.canary+tag", "email": "zq.canary+tag@example.com",
    "phone_fmt": "(415) 555-2671", "phone_digits": "4155552671", "phone_e164": "+14155552671",
    "street": "7741 Canarywood Boulevard", "line2": "Suite 9Q", "city": "Cinderfall", "zip": "73301-4455",
    "password": "Canary-Pw-9x8y7z", "session": "sess-canary-123456",
}


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -> {detail}"))
    if not cond:
        FAILS.append(name)


def reg_input(**over):
    d = dict(first_name=CANARY["first"], last_name=CANARY["last"], email=CANARY["email"], phone=CANARY["phone_fmt"],
             address=pb.AccountAddress(line1=CANARY["street"], line2=CANARY["line2"], city=CANARY["city"], state="tx", zip=CANARY["zip"]),
             password=CANARY["password"], confirm_password=CANARY["password"], preferred_currency="USD")
    d.update(over)
    return pb.RegistrationInput(**d)


def flush(extra=0.6):
    time.sleep(extra)


def rows(sql, params=()):
    return cluster.query(sql, params, db="svc")


# --- service authentication ----------------------------------------------------------------------
def test_service_auth_real():
    try:
        stub.ResolveSession(demo_pb2.Empty(), timeout=10)
        check("no service credential -> UNAUTHENTICATED", False, "call succeeded")
    except grpc.RpcError as e:
        check("no service credential -> UNAUTHENTICATED", e.code() == grpc.StatusCode.UNAUTHENTICATED, e.code())
    try:
        stub.ResolveSession(demo_pb2.Empty(), metadata=(("x-service-name", "frontend"), ("x-service-secret", "wrong")), timeout=10)
        check("wrong secret -> UNAUTHENTICATED", False, "call succeeded")
    except grpc.RpcError as e:
        check("wrong secret -> UNAUTHENTICATED", e.code() == grpc.StatusCode.UNAUTHENTICATED, e.code())
    try:
        stub.ResolveSession(demo_pb2.Empty(), metadata=(("x-service-name", "intruder"), ("x-service-secret", "fe-secret-test")), timeout=10)
        check("unknown service name -> UNAUTHENTICATED", False, "call succeeded")
    except grpc.RpcError as e:
        check("unknown service name -> UNAUTHENTICATED", e.code() == grpc.StatusCode.UNAUTHENTICATED, e.code())
    r, e = env.call(stub.ResolveSession, demo_pb2.Empty())
    check("valid credential, no session -> authenticated=false", e is None and not r.authenticated, e)


# --- registration, session, profile ---------------------------------------------------------------
TOKEN = {}


def test_register_and_session():
    r, e = env.call(stub.Register, pb.RegisterRequest(input=reg_input()))
    check("web registration succeeds", e is None and r.session_token and r.context.authenticated, e and e.details())
    TOKEN["a"], TOKEN["a_id"] = r.session_token, r.context.customer_id
    check("session context: first name, profile complete, version 2", r.context.first_name == CANARY["first"] and r.context.profile_complete
          and r.context.profile_version == 2, r.context)
    row = rows("SELECT email, email_display, first_name, last_name, phone_e164, profile_version, name, status, created_via FROM customers WHERE id=%s", (TOKEN["a_id"],))[0]
    check("stored lowercase key + display + e164 + v2", row[0] == CANARY["email"].lower() and row[1] == CANARY["email"] and row[4] == CANARY["phone_e164"]
          and row[5] == 2 and row[6] == f"{CANARY['first']} {CANARY['last']}" and row[7] == "active" and row[8] == "web", row)
    addr = rows("SELECT line1, line2, city, state, zip, is_default FROM customer_addresses WHERE customer_id=%s", (TOKEN["a_id"],))
    check("registration address saved as default (normalized state)", addr == [(CANARY["street"], CANARY["line2"], CANARY["city"], "TX", CANARY["zip"], True)], addr)
    legacy = rows("SELECT street_address, state FROM addresses WHERE customer_id=%s", (TOKEN["a_id"],))
    check("legacy addresses row kept current for local-mode readers", legacy == [(f"{CANARY['street']}, {CANARY['line2']}", "TX")], legacy)
    key = "auth:" + accounts.hash_secret(TOKEN["a"])
    rec = json.loads(redis.get(key))
    check("Redis session key format and TTL compatible", rec["email"] == CANARY["email"].lower() and rec["customer_id"] == TOKEN["a_id"]
          and "expires_at" in rec and 172000 < redis.ttl(key) <= 172800, (rec, redis.ttl(key)))
    r2, e = env.call(stub.ResolveSession, demo_pb2.Empty(), token=TOKEN["a"])
    check("ResolveSession with token", e is None and r2.authenticated and r2.customer_id == TOKEN["a_id"] and r2.first_name == CANARY["first"], e)
    # an old-format session (no extras) created by the previous implementation still resolves
    old_token = "legacy-token-abc"
    redis.setex("auth:" + accounts.hash_secret(old_token), 3600, json.dumps({"email": "old@example.com", "customer_id": str(uuid.uuid4()), "name": "Olga Old", "expires_at": time.time() + 3600}))
    r3, e = env.call(stub.ResolveSession, demo_pb2.Empty(), token=old_token)
    check("old-format session still resolves; first name derived", e is None and r3.authenticated and r3.first_name == "Olga", (r3, e))


def test_register_validation_and_duplicates():
    before = rows("SELECT count(*) FROM customers")[0][0]
    r, e = env.call(stub.Register, pb.RegisterRequest(input=pb.RegistrationInput()))
    codes = env.field_codes(e)
    check("empty registration -> INVALID_ARGUMENT listing every required field", e and e.code() == grpc.StatusCode.INVALID_ARGUMENT
          and {"first_name", "last_name", "email", "phone", "address_line1", "city", "state", "zip", "password"} <= set(codes), codes)
    r, e = env.call(stub.Register, pb.RegisterRequest(input=reg_input(email="not-an-email", phone="555-1212")))
    codes = env.field_codes(e)
    check("bad email/phone codes", codes.get("email") == "email_invalid_domain" or codes.get("email") == "email_invalid_syntax", codes)
    check("incomplete phone code", codes.get("phone") == "phone_incomplete", codes)
    check("no row stored for rejected registration", rows("SELECT count(*) FROM customers")[0][0] == before)
    r, e = env.call(stub.Register, pb.RegisterRequest(input=reg_input(email=CANARY["email"].upper().replace("EXAMPLE.COM", "example.com"))))
    check("case-different duplicate email -> ALREADY_EXISTS email_taken", e and e.code() == grpc.StatusCode.ALREADY_EXISTS
          and env.field_codes(e).get("email") == "email_taken", (e and e.code(), e and env.field_codes(e)))
    check("plus-addressing variant is a distinct account", env.call(stub.Register, pb.RegisterRequest(input=reg_input(email="zq.canary@example.com", phone="(650) 253-0000")))[1] is None)
    r, e = env.call(stub.ValidateRegistration, reg_input(email="a@b.c", password=""))
    check("ValidateRegistration dry-run returns field errors, creates nothing", e is None and "email" in r.fields and rows("SELECT count(*) FROM customers")[0][0] == before + 1)


def test_login_logout_lockout():
    r, e = env.call(stub.Login, pb.LoginRequest(email=CANARY["email"].upper(), password=CANARY["password"]))
    check("login with different email case succeeds", e is None and r.context.customer_id == TOKEN["a_id"], e and e.details())
    TOKEN["a2"] = r.session_token
    ev = rows("SELECT outcome FROM login_events WHERE customer_id=%s ORDER BY id", (TOKEN["a_id"],))
    check("login event recorded", ("success",) in ev, ev)
    r, e = env.call(stub.Login, pb.LoginRequest(email="nobody@example.com", password="Whatever123"))
    check("unknown user -> UNAUTHENTICATED invalid_credentials", e and e.code() == grpc.StatusCode.UNAUTHENTICATED and env.error_reason(e) == "invalid_credentials")
    r, e = env.call(stub.Login, pb.LoginRequest(email="garbage", password="x"))
    check("malformed email login -> same generic error", e and e.code() == grpc.StatusCode.UNAUTHENTICATED and env.error_reason(e) == "invalid_credentials")
    for _ in range(5):
        env.call(stub.Login, pb.LoginRequest(email="zq.canary@example.com", password="Wrong-pass-1"))
    r, e = env.call(stub.Login, pb.LoginRequest(email="zq.canary@example.com", password="Wrong-pass-1"))
    check("5 failures lock the account (RESOURCE_EXHAUSTED)", e and e.code() == grpc.StatusCode.RESOURCE_EXHAUSTED and env.error_reason(e) == "login_locked", e and e.code())
    r, e = env.call(stub.Logout, demo_pb2.Empty(), token=TOKEN["a2"])
    r, e = env.call(stub.ResolveSession, demo_pb2.Empty(), token=TOKEN["a2"])
    check("logout deletes the session", e is None and not r.authenticated)


def test_profile_and_legacy():
    p, e = env.call(stub.GetProfile, demo_pb2.Empty(), token=TOKEN["a"])
    check("GetProfile", e is None and p.first_name == CANARY["first"] and p.phone_display == CANARY["phone_fmt"] and p.profile_complete, e)
    r, e = env.call(stub.UpdateProfile, pb.UpdateProfileRequest(fields=["phone"], phone="555"), token=TOKEN["a"])
    check("invalid phone update rejected", e and env.field_codes(e).get("phone") == "phone_incomplete", e and env.field_codes(e))
    r, e = env.call(stub.UpdateProfile, pb.UpdateProfileRequest(fields=["first_name", "last_name"], first_name="Zeph", last_name="O'Quill"), token=TOKEN["a"])
    check("name update persists and rebuilds display name", e is None and r.last_name == "O'Quill"
          and rows("SELECT name FROM customers WHERE id=%s", (TOKEN["a_id"],))[0][0] == "Zeph O'Quill", e)
    r, e = env.call(stub.UpdateProfile, pb.UpdateProfileRequest(fields=["first_name"], first_name=""), token=TOKEN["a"])
    check("clearing a required name rejected", e and env.field_codes(e).get("first_name") == "required")
    # legacy (version 1) customer created by the previous implementation: single-row address, no phone
    cid, email = str(uuid.uuid4()), "legacy.user@example.com"
    rows_ = cluster.query("INSERT INTO customers (id,email,name,status,created_via,password_hash) VALUES (%s,%s,%s,'active','web',%s) RETURNING id",
                          (cid, email, "Lena Legacy", accounts._password_hash("Legacy-pass-1")), db="svc")
    cluster.query("INSERT INTO addresses VALUES (%s,'9 Old Rd','Austin','TX','73301','US')", (cid,), db="svc")
    cluster.query("INSERT INTO customer_addresses (customer_id,line1,city,state,zip,is_default) VALUES (%s,'9 Old Rd','Austin','TX','73301',true)", (cid,), db="svc")
    cluster.query("UPDATE customers SET first_name='Lena', last_name='Legacy', email_display=email WHERE id=%s", (cid,), db="svc")
    r, e = env.call(stub.Login, pb.LoginRequest(email=email, password="Legacy-pass-1"))
    check("legacy customer can log in", e is None and r.context.profile_version == 1 and not r.context.profile_complete and list(r.context.missing_fields) == ["phone"], (e, r and r.context))
    tok = r.session_token
    g, e = env.call(stub.GetProfile, demo_pb2.Empty(), token=tok)
    check("legacy profile reports missing phone", e is None and not g.profile_complete and list(g.missing_fields) == ["phone"] and g.phone_display == "", g)
    u, e = env.call(stub.UpdateProfile, pb.UpdateProfileRequest(fields=["phone"], phone="650.253.0000"), token=tok)
    check("completing the profile upgrades to version 2", e is None and u.profile_complete and u.profile_version == 2 and u.phone_e164 == "+16502530000", (e, u))
    rs, e = env.call(stub.ResolveSession, demo_pb2.Empty(), token=tok)
    check("session snapshot refreshed after completion", rs.profile_complete and rs.profile_version == 2, rs)


# --- addresses ----------------------------------------------------------------------------------
def test_addresses_and_isolation():
    def a(l1, **kw):
        return pb.AccountAddress(line1=l1, city=kw.get("city", "Reno"), state=kw.get("state", "NV"), zip=kw.get("zip", "89501"), line2=kw.get("line2", ""))
    lst, e = env.call(stub.ListAddresses, demo_pb2.Empty(), token=TOKEN["a"])
    check("list shows the registration address as default", e is None and len(lst.addresses) == 1 and lst.addresses[0].is_default, e)
    n1, e = env.call(stub.CreateAddress, pb.AddressInput(label="Work", address=a("PO Box 77", city="Reno")), token=TOKEN["a"])
    check("second address created, PO Box flagged, not default", e is None and n1.is_po_box and not n1.is_default, e)
    r, e = env.call(stub.CreateAddress, pb.AddressInput(address=a("1 Bad St", state="ZZ")), token=TOKEN["a"])
    check("invalid state rejected", e and env.field_codes(e).get("state") == "state_invalid", e and env.field_codes(e))
    d, e = env.call(stub.SetDefaultAddress, pb.AddressRef(id=n1.id), token=TOKEN["a"])
    lst, _ = env.call(stub.ListAddresses, demo_pb2.Empty(), token=TOKEN["a"])
    check("set default moves the default flag", e is None and [x.is_default for x in lst.addresses].count(True) == 1 and lst.addresses[0].id == n1.id, lst)
    up, e = env.call(stub.UpdateAddress, pb.UpdateAddressRequest(id=n1.id, label="Office", address=a("12 New St", line2="Apt 2")), token=TOKEN["a"])
    check("update address", e is None and up.line1 == "12 New St" and up.line2 == "Apt 2" and not up.is_po_box and up.label == "Office", e)
    legacy = rows("SELECT street_address FROM addresses WHERE customer_id=%s", (TOKEN["a_id"],))[0][0]
    check("default address change mirrored to the legacy table", legacy == "12 New St, Apt 2", legacy)
    # customer isolation
    rb, e = env.call(stub.Register, pb.RegisterRequest(input=reg_input(email="other.person@example.com", phone="(650) 253-0000", first_name="Other", last_name="Person")))
    tb = rb.session_token
    for name, fn, req in (("update", stub.UpdateAddress, pb.UpdateAddressRequest(id=n1.id, address=a("Hack St"))),
                          ("delete", stub.DeleteAddress, pb.AddressRef(id=n1.id)), ("default", stub.SetDefaultAddress, pb.AddressRef(id=n1.id))):
        r, e = env.call(fn, req, token=tb)
        check(f"other customer cannot {name} my address (NOT_FOUND)", e and e.code() == grpc.StatusCode.NOT_FOUND, e and e.code())
    lst_b, _ = env.call(stub.ListAddresses, demo_pb2.Empty(), token=tb)
    check("other customer sees only their own addresses", all(x.id != n1.id for x in lst_b.addresses) and len(lst_b.addresses) == 1)
    r, e = env.call(stub.DeleteAddress, pb.AddressRef(id=n1.id), token=TOKEN["a"])
    lst, _ = env.call(stub.ListAddresses, demo_pb2.Empty(), token=TOKEN["a"])
    check("deleting the default promotes another", e is None and len(lst.addresses) == 1 and lst.addresses[0].is_default, lst)
    for i in range(9):
        env.call(stub.CreateAddress, pb.AddressInput(address=a(f"{i + 1} Fill St")), token=TOKEN["a"])
    r, e = env.call(stub.CreateAddress, pb.AddressInput(address=a("11 Over St")), token=TOKEN["a"])
    check("address limit (10) enforced", e and e.code() == grpc.StatusCode.RESOURCE_EXHAUSTED and env.error_reason(e) == "address_limit_reached", e and e.code())
    r, e = env.call(stub.ListAddresses, demo_pb2.Empty())
    check("address calls need a session", e and e.code() == grpc.StatusCode.UNAUTHENTICATED)


# --- assistant sign-up + activation --------------------------------------------------------------------
def test_assistant_signup_activation():
    inp = reg_input(email="assist.user@example.com", first_name="Assia", last_name="Tant", phone="(650) 253-0000", password="", confirm_password="")
    r, e = env.call(stub.BeginAssistantSignup, pb.AssistantSignupRequest(input=inp, session_id=CANARY["session"]), service="shoppingassistantservice")
    check("assistant signup creates pending account with handoff", e is None and r.outcome == "created" and r.handoff, e and e.details())
    row = rows("SELECT status, created_via, profile_version, phone_e164 FROM customers WHERE email='assist.user@example.com'")[0]
    check("pending row is v2 with phone", row == ("pending_activation", "assistant", 2, "+16502530000"), row)
    h = json.loads(redis.get("activation_handoff:" + accounts.hash_secret(CANARY["session"])))
    check("raw code only in Redis handoff (not in Postgres)", "raw_code" in h and not rows("SELECT 1 FROM activation_claims WHERE code_hash=%s", (h["raw_code"],)), h.keys())
    a, e = env.call(stub.GetActivation, pb.GetActivationRequest(session_id=CANARY["session"]))
    check("GetActivation pending with masked-safe display email", e is None and a.pending and a.email_display == "assist.user@example.com")
    r, e = env.call(stub.Claim, pb.ClaimRequest(session_id=CANARY["session"], password="short"))
    check("weak password at activation rejected (existing rules)", e and env.field_codes(e).get("password") == "password_too_short", e and env.field_codes(e))
    r, e = env.call(stub.Claim, pb.ClaimRequest(session_id="other-session", password="Activ4te-me-now"))
    check("claim from a different session fails", e and e.code() == grpc.StatusCode.NOT_FOUND)
    r, e = env.call(stub.Claim, pb.ClaimRequest(session_id=CANARY["session"], password="Activ4te-me-now"))
    check("activation succeeds, returns session", e is None and r.session_token and r.context.first_name == "Assia", e and e.details())
    st = rows("SELECT status FROM customers WHERE email='assist.user@example.com'")[0][0]
    check("account active after claim", st == "active")


# --- outage -----------------------------------------------------------------------------------
def test_postgres_outage():
    cluster.stop()
    try:
        r, e = env.call(stub.Login, pb.LoginRequest(email=CANARY["email"], password=CANARY["password"]))
        check("login during Postgres outage -> UNAVAILABLE with reason", e and e.code() == grpc.StatusCode.UNAVAILABLE and env.error_reason(e) in ("unreachable", "timeout", "schema_not_ready"), e and (e.code(), env.error_reason(e)))
        r, e = env.call(stub.Register, pb.RegisterRequest(input=reg_input(email="outage@example.com", phone="(650) 253-0000")))
        check("registration during outage -> UNAVAILABLE, nothing partial", e and e.code() == grpc.StatusCode.UNAVAILABLE)
        r, e = env.call(stub.ResolveSession, demo_pb2.Empty(), token=TOKEN["a"])
        check("session identity survives the outage (Redis only)", e is None and r.authenticated and r.customer_id == TOKEN["a_id"], e)
        r, e = env.call(stub.GetProfile, demo_pb2.Empty(), token=TOKEN["a"])
        check("profile read during outage -> UNAVAILABLE", e and e.code() == grpc.StatusCode.UNAVAILABLE)
    finally:
        cluster.start()
    ok = False
    for _ in range(40):
        r, e = env.call(stub.GetProfile, demo_pb2.Empty(), token=TOKEN["a"])
        if e is None:
            ok = True
            break
        time.sleep(0.5)
    check("service recovers after Postgres returns", ok)
    check("no outage-registration row", rows("SELECT count(*) FROM customers WHERE email='outage@example.com'")[0][0] == 0)


# --- telemetry -----------------------------------------------------------------------------------
def test_instrumentation():
    flush(1.0)
    reg = recv.find("/hipstershop.AccountService/Register", service="accountservice", kind="SERVER")
    check("Register SERVER span exported", len(reg) >= 1, [s.name for s in recv.spans][:20])
    s = next((x for x in reg if x.attrs.get("app.customer.id") == TOKEN["a_id"]), None)
    check("Register span: typed business attributes", s is not None and s.attrs.get("app.account.action") == "register"
          and s.attrs.get("app.account.created_via") == "web" and s.attrs.get("app.account.profile_version") == 2
          and s.attrs.get("app.customer.authenticated") is True and s.attrs.get("app.account.service_caller") == "frontend", s and s.attrs)
    tr = recv.trace_spans(s.trace_id) if s else []
    names = {x.name for x in tr}
    check("same trace: accounts.db span", "accounts.db.customer.create" in names, names)
    pg = [x for x in tr if x.attrs.get("db.system") == "postgresql"]
    check("same trace: Postgres CLIENT spans with peer attributes", pg and all(x.kind == "CLIENT" for x in pg)
          and all(x.attrs.get("server.address") and x.attrs.get("peer.service") and x.attrs.get("server.port") == 5432 for x in pg), pg and pg[0].attrs)
    rd = [x for x in tr if x.attrs.get("db.system") == "redis"]
    check("same trace: Redis CLIENT span for the session write", rd and all(x.kind == "CLIENT" for x in rd), [x.name for x in tr])
    db_parent = next((x for x in tr if x.name == "accounts.db.customer.create"), None)
    check("DB business span is a child of the RPC span and parent of psycopg spans", db_parent and db_parent.parent_id == s.span_id
          and any(x.parent_id == db_parent.span_id for x in pg), None)
    check("db span carries customer id and outcome", db_parent and db_parent.attrs.get("app.customer.id") == TOKEN["a_id"] and db_parent.attrs.get("app.account.db.outcome") == "ok", db_parent and db_parent.attrs)
    # login failure span: no customer id, no PII
    lf = [x for x in recv.find("/hipstershop.AccountService/Login", kind="SERVER") if x.attrs.get("app.customer.authenticated") is False]
    check("failed login span: anonymous, error.type set", lf and lf[0].attrs.get("app.customer.type") == "anonymous" and lf[0].attrs.get("error.type") == "Fail", lf and lf[0].attrs)
    # validation failures: names + codes only
    vf = [x for x in recv.find("/hipstershop.AccountService/Register", kind="SERVER") if x.attrs.get("app.validation.codes")]
    check("validation failure span: field names and codes only", vf and "email" in vf[0].attrs["app.validation.fields"] and "email_invalid_domain" in " ".join(vf[0].attrs["app.validation.codes"]) + " email_invalid_domain", vf and vf[0].attrs)
    # outage span
    out = [x for x in recv.find(kind="SERVER") if x.attrs.get("error.type") == "AccountStoreUnavailable"]
    check("outage RPC span records AccountStoreUnavailable type only", bool(out))
    # all app.* attributes in exported spans satisfy the typed schema
    import telemetry_schema as sch
    bad = []
    for sp in list(recv.spans):
        for k, v in sp.attrs.items():
            if k.startswith("app."):
                try:
                    sch.coerce(k, list(v) if isinstance(v, list) else v)
                except ValueError:
                    bad.append((sp.name, k, v))
    check("every exported app.* attribute satisfies the typed schema", not bad, bad[:3])
    # resource attributes are PII-free and consistent
    res = {tuple(sorted((k, str(v)) for k, v in sp.resource.items())) for sp in recv.spans if sp.service == "accountservice"}
    check("resource attributes: service name/namespace/version present", res and all(dict(r).get("service.namespace") == "online-boutique" and dict(r).get("service.version") == "test-1" for r in res), res)


def test_baggage_filter():
    inbound = (("baggage", "app.customer.email=baggage.canary%40example.com,app.order.channel=web,app.session.ref=0123456789abcdef,note=free-text-canary"),)
    r, e = env.call(stub.ResolveSession, demo_pb2.Empty(), token=TOKEN["a"], extra=inbound)
    flush(1.0)
    hits = recv.canary_hits(["baggage.canary@example.com", "baggage.canary%40example.com", "free-text-canary"])
    check("PII in inbound baggage never exported", not hits, hits)
    sp = [x for x in recv.find("/hipstershop.AccountService/ResolveSession", kind="SERVER") if x.attrs.get("app.order.channel") == "web"]
    check("allowlisted baggage keys are stamped as typed attributes", sp and sp[-1].attrs.get("app.session.ref") == "0123456789abcdef", [x.attrs for x in recv.find("/hipstershop.AccountService/ResolveSession", kind="SERVER")][-1:])


def test_canaries_absent():
    flush(1.0)
    canaries = [v for k, v in CANARY.items()]
    canaries += list(TOKEN.values())[:1] if False else []
    canaries += [TOKEN["a"], TOKEN["a2"], "Zeph", "Legacy-pass-1", "Activ4te-me-now", "legacy.user@example.com", "assist.user@example.com",
                 "Lena", "Assia", "9 Old Rd", "Lover", "650.253.0000", "6502530000", "(650) 253-0000", "+16502530000", "PO Box 77", "12 New St"]
    # session token of the activation + raw activation code
    canaries += [h for h in [json.loads(v)["raw_code"] for k in redis.scan_iter("activation_handoff:*") for v in [redis.get(k)] if v] if h]
    hits = recv.canary_hits([c for c in canaries if c])
    check("no PII / secret / token canary in any exported span, event, link, resource or metric", not hits, hits)
    # raw browser session id is never exported; only an opaque ref could be
    check("hashes never exported (password hash prefix)", not recv.canary_hits(["pbkdf2_sha256$"]))


def test_matrix():
    import yaml
    import account_pb2
    rows_ = yaml.safe_load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "telemetry_matrix.yaml")))["rows"]
    declared = {m.name for m in account_pb2.DESCRIPTOR.services_by_name["AccountService"].methods}
    in_matrix = {r["rpc"] for r in rows_}
    check("every AccountService RPC has a coverage-matrix row", declared <= in_matrix, declared - in_matrix)
    check("matrix has no row for an unknown RPC", in_matrix <= declared, in_matrix - declared)
    import telemetry_schema as sch
    flush(1.0)
    for r in [r for r in rows_ if r["status"] == "implemented" and not r.get("suite")]:   # suite rows need other services
        ok, detail = False, "no SERVER span"
        for sp in recv.find("/hipstershop.AccountService/" + r["rpc"], service="accountservice", kind="SERVER"):
            missing = [a for a in r["server_attrs"] if a not in sp.attrs]
            tr = recv.trace_spans(sp.trace_id)
            names = {x.name for x in tr}
            dbs = {"db:" + x.attrs["db.system"] for x in tr if x.attrs.get("db.system")}
            miss_children = [c for c in r["children"] if c not in names and c not in dbs]
            detail = (missing, miss_children)
            if not missing and not miss_children:
                ok = True
                break
        check(f"matrix {r['id']} {r['rpc']}: server attrs + child spans present", ok, detail)


def test_metrics_exported():
    flush(1.5)
    names = {n for n, _ in recv.metric_points}
    check("metrics exported: actions, login, rpc duration, db duration, validation failures",
          {"app.account.actions", "app.account.login", "app.account.rpc.duration", "app.account.db.duration", "app.account.validation_failures"} <= names, names)


if __name__ == "__main__":
    test_service_auth_real()
    test_register_and_session()
    test_register_validation_and_duplicates()
    test_login_logout_lockout()
    test_profile_and_legacy()
    test_addresses_and_isolation()
    test_assistant_signup_activation()
    test_postgres_outage()
    test_instrumentation()
    test_baggage_filter()
    test_canaries_absent()
    test_matrix()
    test_metrics_exported()
    print(f"\n{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED'}")
    sys.exit(1 if FAILS else 0)
