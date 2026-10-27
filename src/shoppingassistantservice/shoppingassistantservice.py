"""Shopping assistant service (Amazon Bedrock tool-calling agent).

Contract with the frontend is unchanged: POST / {message, ...} -> {content, ...}.
Optional request fields: session_id, prompt_category, conversation_id. The same
values are also accepted as X-Session-ID / X-Prompt-Category / X-Conversation-ID
headers. The `image` field the old Gemini UI sent is ignored.
"""
import os

from flask import Flask, jsonify, request

import accounts
import agent
import config
import telemetry as tel

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024  # the legacy UI may still POST a base64 image
exporting = tel.init(app)


def _field(body, key, header, default=""):
    return str(body.get(key) or request.headers.get(header) or default).strip()


@app.get("/healthz")
def healthz():
    return "ok", 200


@app.before_request
def _legacy_account_api_closed():
    """ACCOUNT_BACKEND=grpc: the frontend talks to accountservice; these HTTP endpoints must not exist."""
    if config.ACCOUNT_BACKEND == "grpc" and request.path.startswith(("/account/", "/readyz/accounts")):
        return jsonify({"error": "not_found"}), 404


def _grpc_identity(token):
    """-> (customer_account|None, profile_available). Identity comes from accountservice only."""
    import account_client
    client = accounts.grpc_client()
    if not token:
        return None, True
    try:
        who = client.resolve(token)
    except account_client.AccountUnavailable:
        return None, False
    if not who:
        return None, True
    base = {"customer_id": who["customer_id"], "name": who["first_name"], "email": "", "address": None,
            "preferred_currency": who["preferred_currency"]}
    try:
        prof = client.profile(token)
        addrs = client.addresses(token)
    except account_client.AccountUnavailable:
        return base, False            # still signed in; saved details unavailable
    default = next((a for a in addrs if a["is_default"]), addrs[0] if addrs else None)
    base.update(name=f"{prof['first_name']} {prof['last_name']}".strip(), email=prof["email"])
    if default:
        base["address"] = {"street_address": default["line1"] + (f", {default['line2']}" if default["line2"] else ""),
                           "city": default["city"], "state": default["state"], "zip_code": default["zip"], "country": default["country"]}
    return base, True


def _internal_ok():
    expected = os.environ.get("ACCOUNT_API_SECRET", "")
    return bool(expected) and request.headers.get("X-Account-Api-Secret") == expected


def _internal_denied():
    # Distinct from a user's expired session: this means frontend/assistant ACCOUNT_API_SECRET differ.
    tel.warn("account.internal_auth_failed", **{"app.account.secret_configured": bool(os.environ.get("ACCOUNT_API_SECRET")),
                                                "app.account.secret_sent": bool(request.headers.get("X-Account-Api-Secret"))})
    return jsonify({"error": "internal_auth_failed"}), 503


def _account_response(acct):
    return accounts.AccountStore.public_profile(acct)


def _account_outcome(operation, outcome, acct=None, email="", error_fields=()):
    """Span attrs + metric + log for a website account operation. Synthetic identity only;
    passwords, addresses and session tokens are never recorded."""
    # No email, name or hash is exported: only the outcome and, for a known customer, the Postgres UUID.
    attrs = {"app.account.operation": operation, "app.account.outcome": outcome,
             "app.account.channel": "web", "app.customer.synthetic": True}
    cid = tel.customer_uuid((acct or {}).get("customer_id") or (acct or {}).get("id"))
    if cid:
        attrs["app.customer.id"] = cid
    if error_fields:
        attrs["app.account.error_fields"] = ",".join(sorted(error_fields))
    span = tel.trace.get_current_span()
    for k, v in attrs.items():
        span.set_attribute(k, v)
    tel.account_web_operations.add(1, {"operation": operation, "outcome": outcome, "channel": "web"})
    (tel.event if outcome == "success" else tel.warn)(f"account.{operation}", **attrs)


def _validation_error(operation, errors, email=""):
    _account_outcome(operation, "validation_failed", email=email, error_fields=errors.keys())
    return jsonify({"error": "validation_failed", "fields": errors}), 400


@app.errorhandler(accounts.AccountStoreUnavailable)
def _account_store_unavailable(e):
    span = tel.trace.get_current_span()
    span.set_attribute("app.account.db.outcome", e.reason)
    tel.account_web_operations.add(1, {"operation": request.path.rsplit("/", 1)[-1], "outcome": "unavailable", "channel": "web"})
    return jsonify({"error": "account_store_unavailable", "reason": e.reason}), 503


@app.get("/readyz/accounts")
def readyz_accounts():
    """Diagnostics only (not a probe): schema/instance state of the accounts database."""
    try:
        repo = accounts.store.repo
    except accounts.AccountStoreUnavailable:
        return jsonify({"state": "not_configured"}), 200
    sm = repo.schema
    return jsonify({"state": sm.state, "version": sm.version, "failure": sm.failure}), 200 if sm.state == "ready" else 503


@app.post("/account/register")
def account_register():
    if not _internal_ok():
        return _internal_denied()
    body = request.get_json(silent=True) or {}
    profile, password, errors = accounts.validate_registration(body)
    if errors:
        return _validation_error("register", errors, profile["email"] if "email" not in errors else "")
    created, acct = accounts.store.register(profile, password)
    if not created:
        _account_outcome("register", "exists", email=profile["email"])
        return jsonify({"error": "account_exists",
                        "fields": {"email": "An account with this email already exists."}}), 409
    _account_outcome("register", "success", acct)
    try:
        token = accounts.store.create_session(acct["email"], acct["id"], acct["name"])
    except Exception:  # noqa: BLE001 - account exists; the user just needs to log in
        return jsonify({"account": _account_response(acct), "login_required": True}), 201
    return jsonify({"account": _account_response(acct), "session_token": token}), 201


@app.get("/account/activation")
def account_activation():
    """Never returns the activation code: it stays in the server-side Redis handoff."""
    if not _internal_ok():
        return _internal_denied()
    status = accounts.store.activation_status(_field({}, "session_id", "X-Session-ID"))
    return jsonify(status), (200 if status["pending"] else 404)


@app.post("/account/claim")
def account_claim():
    if not _internal_ok():
        return _internal_denied()
    body = request.get_json(silent=True) or {}
    password = str(body.get("password") or "")
    password_err = accounts.validate_password(password)
    if not password_err and "confirm_password" in body and str(body.get("confirm_password")) != password:
        return _validation_error("activation", {"confirm_password": "Passwords don't match."})
    if password_err:
        return _validation_error("activation", {"password": password_err})
    acct, token = accounts.store.claim(str(body.get("session_id") or ""), password)
    if not acct:
        _account_outcome("activation", "failed")
        return jsonify({"error": "activation_failed"}), 400
    _account_outcome("activation", "success", acct)
    if not token:
        return jsonify({"activated": True, "login_required": True}), 200
    return jsonify({"account": _account_response(accounts.store.get(acct["email"]) or acct), "session_token": token})


@app.post("/account/login")
def account_login():
    if not _internal_ok():
        return _internal_denied()
    body = request.get_json(silent=True) or {}
    email = accounts.normalize_email(body.get("email"))
    password = str(body.get("password") or "")
    errors = {}
    if accounts.validate_email(email):
        errors["email"] = accounts.validate_email(email)
    if not password:
        errors["password"] = "Enter your password."
    if errors:
        return _validation_error("login", errors)
    if accounts.store.login_locked(email):
        _account_outcome("login", "locked", email=email)
        return jsonify({"error": "too_many_attempts"}), 429
    acct = accounts.store.authenticate(email, password)
    if not acct:
        # One message for unknown, pending and wrong-password: don't reveal which accounts exist.
        failures = accounts.store.record_login_failure(email)
        _account_outcome("login", "invalid_credentials", email=email)
        return jsonify({"error": "invalid_credentials",
                        "attempts_remaining": max(accounts.LOGIN_MAX_FAILURES - failures, 0)}), 401
    accounts.store.clear_login_failures(email)
    accounts.store.record_login_event(acct, "login")
    _account_outcome("login", "success", acct)
    return jsonify({"account": _account_response(acct),
                    "session_token": accounts.store.create_session(acct["email"], acct["id"], acct["name"])})


@app.get("/account/profile")
def account_profile_get():
    if not _internal_ok():
        return _internal_denied()
    ident = accounts.store.session_account(request.args.get("session_token") or request.headers.get("X-Account-Session"))
    if not ident:
        return jsonify({"error": "unauthorized"}), 401
    acct = accounts.store.get(ident["email"])  # may raise AccountStoreUnavailable -> 503, session kept
    return jsonify({"account": _account_response(acct)}) if acct else (jsonify({"error": "unauthorized"}), 401)


@app.post("/account/profile")
def account_profile():
    if not _internal_ok():
        return _internal_denied()
    body = request.get_json(silent=True) or {}
    ident = accounts.store.session_account(body.get("session_token"))
    if not ident:
        _account_outcome("profile_update", "unauthorized")
        return jsonify({"error": "unauthorized"}), 401
    name, name_errors = accounts.validate_person_name(body)
    address, errors = accounts.validate_address(body)
    errors.update(name_errors)
    currency, currency_err = accounts.validate_currency(body.get("preferred_currency"))
    if currency_err:
        errors["preferred_currency"] = currency_err
    if errors:
        return _validation_error("profile_update", errors, ident["email"])
    acct = accounts.store.update(ident["email"], name=name, address=address, preferred_currency=currency)
    if not acct:
        return jsonify({"error": "unauthorized"}), 401
    _account_outcome("profile_update", "success", acct)
    return jsonify({"account": _account_response(acct)})


@app.get("/account/context")
def account_context():
    if not _internal_ok():
        return _internal_denied()
    ident = accounts.store.session_account(request.headers.get("X-Account-Session"))
    return jsonify({"account": _account_response(ident) if ident else None})


@app.post("/account/logout")
def account_logout():
    if not _internal_ok():
        return _internal_denied()
    acct = accounts.store.session_account(request.headers.get("X-Account-Session"))
    accounts.store.delete_session(request.headers.get("X-Account-Session"))
    _account_outcome("logout", "success", acct)
    return jsonify({"ok": True})


@app.post("/")
def chat():
    body = request.get_json(silent=True) or {}
    message = str(body.get("message") or "").strip()
    if not message:
        return jsonify({"content": "Please tell me what you're looking for.", "error": "empty message"}), 400

    session_id = _field(body, "session_id", "X-Session-ID", "anonymous")[:64]
    category = _field(body, "prompt_category", "X-Prompt-Category", "uncategorized")[:48]
    # Never default the conversation to the raw session id: use the opaque reference instead.
    conversation = _field(body, "conversation_id", "X-Conversation-ID", "")[:64] or tel.session_ref(session_id) or tel.opaque_fallback(session_id)

    # Make the customer identity travel on every outgoing gRPC call, like the
    # frontend does for app.session.id.
    auth_token = request.headers.get("X-Account-Session", "")
    if config.ACCOUNT_BACKEND == "grpc":
        public, profile_available = _grpc_identity(auth_token)
        account = public
        bag = {"app.session.ref": tel.session_ref(session_id), "app.prompt.category": category,
               "app.customer.authenticated": bool(public)}
        if public:
            bag["app.customer.id"] = public["customer_id"]
        token = tel.attach_baggage(**bag)
    else:
        account = accounts.store.session_account(auth_token)  # identity from Redis only
        profile_available = True
        if account:
            try:
                profile = accounts.store.get(account["email"])
                if profile:
                    account = profile
            except accounts.AccountStoreUnavailable:
                profile_available = False  # keep the logged-in identity; never downgrade to guest
        legacy_id = tel.customer_uuid((account or {}).get("id"))
        token = tel.attach_baggage(**{**tel.identity_attrs(session_id, legacy_id), "app.prompt.category": category})
    try:
        session = agent.get_session(session_id)
        session.customer_account = account if config.ACCOUNT_BACKEND == "grpc" else accounts.AccountStore.public_profile(account)
        session.profile_available = profile_available
        # Forwarded to checkout so orders are attributed server-side; never logged or traced.
        session.account_token = auth_token if (config.ACCOUNT_BACKEND == "grpc" and account) else ""
        session.checkout.customer_id = tel.customer_uuid((account or {}).get("customer_id")) if config.ACCOUNT_BACKEND == "grpc" else None
        if account:
            session.pending_activation = False  # a logged-in customer has nothing left to activate
        with session.lock:  # one in-flight prompt per conversation
            result = agent.run(session, message, category, conversation)
    finally:
        tel.detach_baggage(token)

    status = 200
    outcome = result["details"]["outcome"]
    if outcome == "rate_limited":
        status = 429
    elif outcome == "not_configured":
        status = 503
    return jsonify(result), status


if __name__ == "__main__":
    if config.ACCOUNT_BACKEND != "grpc":
        accounts.store.start()
    tel.event("assistant.starting", **{
        "llm.provider": "aws.bedrock", "bedrock.model_id": config.BEDROCK_MODEL_ID,
        "aws.region": config.AWS_REGION,
        "prompt.version": config.PROMPT_VERSION, "telemetry.exporting": exporting,
        "fault.injection": sorted(config.FAULT_INJECTION)})
    app.run(host="0.0.0.0", port=config.PORT, threaded=True)
