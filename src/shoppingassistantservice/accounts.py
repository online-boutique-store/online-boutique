"""Customer accounts + synthetic payment methods.

Postgres (accounts_db.py) is the source of truth for customers, addresses, hashed activation
claims and login audit. Redis keeps authenticated sessions (with an identity snapshot so a
Postgres outage never turns a logged-in customer into an anonymous guest), login lockouts and the
short-lived, server-only activation handoff that carries the raw activation code.

Payment methods are a fixed catalog of stock TEST cards. The model and the customer
only ever see the id and label ("Visa ending 0454"); card numbers are used solely
inside backends.place_order and never appear in prompts, tool args/results, spans
or logs.
"""
import hashlib
import hmac
import json
import re
import secrets
import time

import accounts_db
import config
import telemetry as tel
import telemetry_sanitize

PAYMENT_METHODS = {
    "pm_demo_visa": {"label": "Visa ending 0454", "last4": "0454", "number": "4432-8015-6152-0454"},
    "pm_demo_visa_alt": {"label": "Visa ending 1111", "last4": "1111", "number": "4111-1111-1111-1111"},
    "pm_demo_mastercard": {"label": "Mastercard ending 4444", "last4": "4444", "number": "5555-5555-5555-4444"},
}
DEFAULT_PAYMENT_METHOD = "pm_demo_visa"


def list_payment_methods():
    return [{"id": k, "label": v["label"]} for k, v in PAYMENT_METHODS.items()]


def card_number(payment_method_id):
    return PAYMENT_METHODS[payment_method_id]["number"]


def normalize_email(email):
    return str(email or "").strip().lower()


def email_hash(email):
    return hashlib.sha256(normalize_email(email).encode()).hexdigest()[:12]


CLAIM_TTL_SECONDS = 900
SESSION_TTL_SECONDS = 172800
LOGIN_MAX_FAILURES = 5
LOGIN_LOCKOUT_SECONDS = 900

SUPPORTED_CURRENCIES = ("USD", "EUR", "CAD", "JPY", "GBP", "TRY")
ADDRESS_FIELDS = ("street_address", "city", "state", "zip_code", "country")
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$")
_NAME_RE = re.compile(r"^[^\W\d_]+([ '.-][^\W\d_]+)*\.?$")
_US_ZIP_RE = re.compile(r"^\d{5}(-\d{4})?$")
_POSTAL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 -]{1,9}$")
_US_NAMES = {"us", "usa", "united states", "united states of america"}


def _clean(value, limit=120):
    return " ".join(str(value or "").split())[:limit]


def validate_name(name, label="name"):
    if not name:
        return f"Enter your {label}."
    if len(name) > 60:
        return f"{label.capitalize()} must be 60 characters or fewer."
    if not _NAME_RE.match(name):
        return f"{label.capitalize()} can only contain letters, spaces, apostrophes, periods and hyphens."
    return None


def validate_person_name(fields):
    """Required first + last name -> (full_name, errors). Stored as one display name."""
    first = _clean(fields.get("first_name"), 40)
    last = _clean(fields.get("last_name"), 40)
    errors = {}
    for key, value, label in (("first_name", first, "first name"), ("last_name", last, "last name")):
        err = validate_name(value, label)
        if err:
            errors[key] = err
    return f"{first} {last}".strip(), errors


def split_name(name):
    parts = str(name or "").strip().split(" ", 1)
    return parts[0], (parts[1] if len(parts) > 1 else "")


def validate_email(email):
    if not email:
        return "Enter your email address."
    if len(email) > 254 or not _EMAIL_RE.match(email):
        return "Enter a valid email address, like name@example.com."
    return None


def validate_password(password, email=""):
    if not password:
        return "Enter a password."
    if len(password) < 8:
        return "Password must be at least 8 characters."
    if len(password) > 128:
        return "Password must be 128 characters or fewer."
    if not re.search(r"[A-Za-z]", password) or not re.search(r"\d", password):
        return "Password must include at least one letter and one number."
    if email and password.strip().lower() == normalize_email(email):
        return "Password can't be the same as your email."
    return None


def validate_address(fields):
    """Returns (address, errors). Every field is required; postal code is checked per country."""
    telemetry_sanitize.register_address(fields)
    address = {k: _clean(fields.get(k)) for k in ADDRESS_FIELDS}
    labels = {"street_address": "street address", "city": "city", "state": "state or region",
              "zip_code": "postal code", "country": "country"}
    errors = {k: f"Enter your {labels[k]}." for k, v in address.items() if not v}
    zip_code, country = address["zip_code"], address["country"]
    if zip_code and "zip_code" not in errors:
        if country.lower() in _US_NAMES:
            if not _US_ZIP_RE.match(zip_code):
                errors["zip_code"] = "Enter a 5-digit US ZIP code (for example 45202)."
        elif not _POSTAL_RE.match(zip_code):
            errors["zip_code"] = "Enter a valid postal code."
    if address["street_address"] and len(address["street_address"]) < 3:
        errors["street_address"] = "Enter a full street address."
    return address, errors


def validate_currency(code):
    code = str(code or "USD").strip().upper()
    return code, (None if code in SUPPORTED_CURRENCIES else "Choose a supported currency.")


def validate_registration(body):
    """Returns (profile, password, errors). `errors` maps form field -> message."""
    name, name_errors = validate_person_name(body)
    email = normalize_email(body.get("email"))
    password = str(body.get("password") or "")
    confirm = body.get("confirm_password")
    address, errors = validate_address(body)
    errors.update(name_errors)
    currency, currency_err = validate_currency(body.get("preferred_currency"))
    for field, err in (("email", validate_email(email)),
                       ("password", validate_password(password, email)),
                       ("preferred_currency", currency_err)):
        if err:
            errors[field] = err
    if confirm is not None and str(confirm) != password and "password" not in errors:
        errors["confirm_password"] = "Passwords don't match."
    profile = {"name": name, "email": email, "address": address, "preferred_currency": currency}
    return profile, password, errors


def _hash_secret(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def _password_hash(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", str(password).encode(), salt, 200000)
    return "pbkdf2_sha256$200000$" + secrets.token_urlsafe(12) + "$" + salt.hex() + "$" + digest.hex()


def _password_check(password, stored):
    try:
        _, rounds, _, salt_hex, digest_hex = stored.split("$", 4)
        digest = hashlib.pbkdf2_hmac("sha256", str(password).encode(), bytes.fromhex(salt_hex), int(rounds))
        return hmac.compare_digest(digest.hex(), digest_hex)
    except (ValueError, TypeError):
        return False


AccountStoreUnavailable = accounts_db.AccountStoreUnavailable


class Identity(dict):
    """Who is logged in, resolved from Redis only: customer_id, email, name."""


class AccountStore:
    """Public account API. `client` (Redis) and `repo` (Postgres) are injectable for tests."""

    def __init__(self, client=None, repo=None):
        self._client = client
        self._repo = repo

    # --- plumbing -------------------------------------------------------------------------
    def _r(self):
        if self._client is None:
            import redis
            host, _, port = config.REDIS_ADDR.partition(":")
            self._client = redis.Redis(host=host, port=int(port or 6379), decode_responses=True,
                                       socket_connect_timeout=2, socket_timeout=2)
        return self._client

    @property
    def repo(self):
        if self._repo is None:
            if not config.ACCOUNTS_DATABASE_URL:
                raise AccountStoreUnavailable("unreachable")
            self._repo = accounts_db.PgAccountRepository()
        return self._repo

    def start(self):
        """Begin background schema initialisation (never blocks, never raises)."""
        try:
            self.repo.start()
        except AccountStoreUnavailable:
            pass

    # --- customers (Postgres) ----------------------------------------------------------------
    def get(self, email):
        return self.repo.get_customer(normalize_email(email))

    def register(self, profile, password):
        """Web sign-up. -> (created, account)."""
        email = normalize_email(profile["email"])
        outcome, row = self.repo.register_web(email, profile["name"], profile["address"], _password_hash(password),
                                              profile.get("preferred_currency", "USD"))
        if outcome != "created":
            return False, None
        return True, dict(profile, email=email, id=row["id"], status="active")

    def update(self, email, **fields):
        email = normalize_email(email)
        ok = self.repo.update_customer(
            email, name=fields.get("name"), preferred_currency=fields.get("preferred_currency"),
            default_payment_method_id=fields.get("default_payment_method_id"), address=fields.get("address") or None)
        return self.repo.get_customer(email) if ok else None

    def authenticate(self, email, password):
        acct = self.repo.get_customer(normalize_email(email))
        if not acct or acct["status"] != "active" or not acct.get("password_hash"):
            # Same work as a real check so response time doesn't reveal unknown or pending emails.
            _password_check(password, "pbkdf2_sha256$200000$x$00$00")
            return None
        return acct if _password_check(password, acct["password_hash"]) else None

    def record_login_event(self, acct, outcome):
        try:
            self.repo.record_login_event(acct["id"] if acct else None, outcome)
        except AccountStoreUnavailable:
            pass  # audit is best effort; never blocks a successful login

    # --- chat account creation + activation (protocol 4.1-4.3) --------------------------------------
    def _handoff_key(self, session_id):
        return f"activation_handoff:{_hash_secret(session_id)}"

    def _write_handoff(self, session_id, customer_id, email, code):
        self._r().setex(self._handoff_key(session_id), CLAIM_TTL_SECONDS,
                        json.dumps({"customer_id": customer_id, "email": email, "raw_code": code}))

    def create_pending(self, session_id, name, email, address):
        """-> {"outcome", "customer_id", "handoff": bool}. The raw code goes ONLY to Redis."""
        email = normalize_email(email)
        code = secrets.token_urlsafe(24)
        outcome, cid = self.repo.create_pending(email, name, address, _hash_secret(session_id), _hash_secret(code))
        handoff = False
        if outcome in ("created", "resumed", "recreated_abandoned"):
            tel.account_activation_events.add(1, {"outcome": outcome})
            try:
                self._write_handoff(session_id, cid, email, code)
                handoff = True
            except Exception as e:  # noqa: BLE001 - Postgres is authoritative; regenerate later
                tel.warn("account.handoff_failed", **{"error.type": type(e).__name__, "app.account.handoff.outcome": "failed"})
        else:
            tel.account_activation_events.add(1, {"outcome": outcome})
        return {"outcome": outcome, "customer_id": cid, "handoff": handoff}

    def activation_status(self, session_id):
        """-> {"pending": bool, "email": str}. Regenerates the handoff for THIS session's own claim."""
        raw = self._r().get(self._handoff_key(session_id))
        if raw:
            handoff = json.loads(raw)
            # Redis is only a delivery cache. Do not report a stale handoff after the
            # pending account was abandoned/recreated or activated.
            if self.repo.claim_is_live(handoff["customer_id"], _hash_secret(session_id)):
                return {"pending": True, "email": handoff["email"]}
            self._r().delete(self._handoff_key(session_id))
        code = secrets.token_urlsafe(24)
        row = self.repo.regenerate_claim(_hash_secret(session_id), _hash_secret(code))
        if not row:
            return {"pending": False}
        self._write_handoff(session_id, row["id"], row["email"], code)
        tel.account_activation_events.add(1, {"outcome": "regenerated"})
        return {"pending": True, "email": row["email"]}

    def claim(self, session_id, password):
        """-> (account|None, session_token|None). Fails closed with no detail on any mismatch."""
        raw = self._r().get(self._handoff_key(session_id))
        if not raw:
            self.activation_status(session_id)  # regenerate for this session; caller resubmits
            return None, None
        h = json.loads(raw)
        acct = self.repo.consume_claim(h["customer_id"], _hash_secret(session_id), _hash_secret(h["raw_code"]),
                                       _password_hash(password))
        if not acct:
            tel.account_activation_events.add(1, {"outcome": "failed"})
            return None, None
        tel.account_activation_events.add(1, {"outcome": "consumed"})
        token = None
        try:
            self._r().delete(self._handoff_key(session_id))
            token = self.create_session(acct["email"], acct["id"], acct["name"])
        except Exception:  # noqa: BLE001 - account is already active; caller reports login_required
            token = None
        return acct, token

    # --- login throttling (Redis) ---------------------------------------------------------------
    @staticmethod
    def _fail_key(email):
        return f"login_fail:{email_hash(email)}"

    def login_locked(self, email):
        raw = self._r().get(self._fail_key(email))
        if not raw:
            return False
        record = json.loads(raw)
        return record.get("count", 0) >= LOGIN_MAX_FAILURES and record.get("until", 0) > time.time()

    def record_login_failure(self, email):
        raw = self._r().get(self._fail_key(email))
        record = json.loads(raw) if raw else {"count": 0}
        if record.get("until", 0) and record["until"] <= time.time():
            record = {"count": 0}
        record["count"] = record.get("count", 0) + 1
        record["until"] = time.time() + LOGIN_LOCKOUT_SECONDS
        self._r().setex(self._fail_key(email), LOGIN_LOCKOUT_SECONDS, json.dumps(record))
        return record["count"]

    def clear_login_failures(self, email):
        self._r().delete(self._fail_key(email))

    # --- sessions (Redis; identity snapshot survives a Postgres outage) ----------------------------
    def create_session(self, email, customer_id=None, name=None):
        token = secrets.token_urlsafe(32)
        value = json.dumps({"email": normalize_email(email), "customer_id": customer_id, "name": name,
                            "expires_at": time.time() + SESSION_TTL_SECONDS})
        self._r().setex(f"auth:{_hash_secret(token)}", SESSION_TTL_SECONDS, value)
        return token

    def session_account(self, token):
        """Identity only - never touches Postgres."""
        if not token:
            return None
        raw = self._r().get(f"auth:{_hash_secret(token)}")
        if not raw:
            return None
        record = json.loads(raw)
        if record.get("expires_at", 0) <= time.time():
            self.delete_session(token)
            return None
        return Identity(customer_id=record.get("customer_id"), email=record["email"], name=record.get("name") or "")

    def delete_session(self, token):
        if token:
            self._r().delete(f"auth:{_hash_secret(token)}")

    @staticmethod
    def public_profile(acct):
        if not acct:
            return None
        out = {k: acct.get(k) for k in ("name", "email", "address", "preferred_currency", "created_at")}
        out["first_name"], out["last_name"] = split_name(acct.get("name"))
        return out


store = AccountStore()
