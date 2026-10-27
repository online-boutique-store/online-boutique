"""Customer accounts: sessions, lockouts, activation handoff, password hashing, orchestration.

Postgres (accounts_db.py) is the source of truth for customers, addresses, hashed activation claims
and login audit. Redis keeps authenticated sessions (an identity snapshot so a Postgres outage never
turns a logged-in customer into an anonymous guest), login lockouts and the short-lived, server-only
activation handoff that carries the raw activation code.

Redis key formats and TTLs are byte-compatible with the previous in-assistant implementation, so
sessions created before the cutover keep working:
  auth:{sha256(token)}                      JSON {email, customer_id, name, expires_at, ...extras}   172800s
  activation_handoff:{sha256(session_id)}   JSON {customer_id, email, raw_code}                       900s
  login_fail:{sha256(email)[:12]}           JSON {count, until}                                       900s
"""
import hashlib
import hmac
import json
import secrets
import time

import accounts_db
import config
import telemetry as tel
import validation

AccountStoreUnavailable = accounts_db.AccountStoreUnavailable


def hash_secret(value):
    return hashlib.sha256(str(value).encode()).hexdigest()


def email_hash12(email):
    return hashlib.sha256(str(email or "").strip().lower().encode()).hexdigest()[:12]


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


class Identity(dict):
    """Who is logged in, resolved from Redis only: customer_id, email, name (+ snapshot extras)."""


class AccountStore:
    """`client` (Redis) and `repo` (Postgres) are injectable for tests."""

    def __init__(self, client=None, repo=None):
        self._client = client
        self._repo = repo

    # plumbing ---------------------------------------------------------------------------------
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
        try:
            self.repo.start()
        except AccountStoreUnavailable:
            pass

    # customers --------------------------------------------------------------------------------
    def register(self, profile, password):
        """Web sign-up -> (created, customer_id|None)."""
        outcome, cid = self.repo.register_web(profile, _password_hash(password))
        return outcome == "created", cid

    def authenticate(self, email_key, password):
        acct = self.repo.get_customer_by_email(email_key)
        if not acct or acct["status"] != "active" or not acct.get("password_hash"):
            _password_check(password, "pbkdf2_sha256$200000$x$00$00")     # same work: no account enumeration by timing
            return None
        return acct if _password_check(password, acct["password_hash"]) else None

    def record_login_event(self, customer_id, outcome):
        try:
            self.repo.record_login_event(customer_id, outcome)
        except AccountStoreUnavailable:
            pass      # audit is best effort; never blocks a successful login

    # assistant sign-up + activation -----------------------------------------------------------
    @staticmethod
    def _handoff_key(session_id):
        return f"activation_handoff:{hash_secret(session_id)}"

    def _write_handoff(self, session_id, customer_id, email_display, code):
        self._r().setex(self._handoff_key(session_id), config.CLAIM_TTL_SECONDS,
                        json.dumps({"customer_id": customer_id, "email": email_display, "raw_code": code}))

    def create_pending(self, session_id, profile):
        """-> {"outcome", "customer_id", "handoff"}. The raw code goes ONLY to Redis."""
        code = secrets.token_urlsafe(24)
        outcome, cid = self.repo.create_pending(profile, hash_secret(session_id), hash_secret(code))
        handoff = False
        tel.account_activation_events.add(1, {"outcome": outcome})
        if outcome in ("created", "resumed", "recreated_abandoned"):
            try:
                self._write_handoff(session_id, cid, profile["email_display"], code)
                handoff = True
            except Exception as e:  # noqa: BLE001 - Postgres is authoritative; regenerate later
                tel.warn("account.handoff_failed", **{"error.type": type(e).__name__, "app.account.handoff.outcome": "failed"})
        return {"outcome": outcome, "customer_id": cid, "handoff": handoff}

    def activation_status(self, session_id):
        raw = self._r().get(self._handoff_key(session_id))
        if raw:
            handoff = json.loads(raw)
            if self.repo.claim_is_live(handoff["customer_id"], hash_secret(session_id)):
                return {"pending": True, "email_display": handoff["email"]}
            self._r().delete(self._handoff_key(session_id))
        code = secrets.token_urlsafe(24)
        row = self.repo.regenerate_claim(hash_secret(session_id), hash_secret(code))
        if not row:
            return {"pending": False}
        cust = self.repo.get_customer(row["id"])
        display = (cust or {}).get("email_display") or row["email"]
        self._write_handoff(session_id, row["id"], display, code)
        tel.account_activation_events.add(1, {"outcome": "regenerated"})
        return {"pending": True, "email_display": display}

    def claim(self, session_id, password):
        """-> (account|None, session_token|None). Fails closed with no detail on any mismatch."""
        raw = self._r().get(self._handoff_key(session_id))
        if not raw:
            self.activation_status(session_id)
            return None, None
        h = json.loads(raw)
        acct = self.repo.consume_claim(h["customer_id"], hash_secret(session_id), hash_secret(h["raw_code"]),
                                       _password_hash(password))
        if not acct:
            tel.account_activation_events.add(1, {"outcome": "failed"})
            return None, None
        tel.account_activation_events.add(1, {"outcome": "consumed"})
        token = None
        try:
            self._r().delete(self._handoff_key(session_id))
            token = self.create_session(acct)
        except Exception:  # noqa: BLE001 - account is active; caller reports login_required
            token = None
        return acct, token

    # login throttling -------------------------------------------------------------------------
    @staticmethod
    def _fail_key(email):
        return f"login_fail:{email_hash12(email)}"

    def login_locked(self, email):
        raw = self._r().get(self._fail_key(email))
        if not raw:
            return False
        record = json.loads(raw)
        return record.get("count", 0) >= config.LOGIN_MAX_FAILURES and record.get("until", 0) > time.time()

    def record_login_failure(self, email):
        raw = self._r().get(self._fail_key(email))
        record = json.loads(raw) if raw else {"count": 0}
        if record.get("until", 0) and record["until"] <= time.time():
            record = {"count": 0}
        record["count"] = record.get("count", 0) + 1
        record["until"] = time.time() + config.LOGIN_LOCKOUT_SECONDS
        self._r().setex(self._fail_key(email), config.LOGIN_LOCKOUT_SECONDS, json.dumps(record))
        return record["count"]

    def clear_login_failures(self, email):
        self._r().delete(self._fail_key(email))

    # sessions (identity snapshot survives a Postgres outage) -----------------------------------
    def create_session(self, acct):
        token = secrets.token_urlsafe(32)
        self._r().setex(f"auth:{hash_secret(token)}", config.SESSION_TTL_SECONDS,
                        json.dumps(self._snapshot(acct)))
        return token

    @staticmethod
    def _snapshot(acct, expires_at=None):
        missing = validation.missing_profile_fields(acct)
        return {"email": acct["email"], "customer_id": acct["id"], "name": acct.get("name") or "",
                "first_name": acct.get("first_name") or (acct.get("name") or "").split(" ")[0],
                "profile_version": acct.get("profile_version", 1), "profile_complete": not missing and acct.get("profile_version", 1) == 2,
                "preferred_currency": acct.get("preferred_currency", "USD"),
                "expires_at": expires_at or time.time() + config.SESSION_TTL_SECONDS}

    def refresh_session(self, token, acct):
        """After a profile/address change keep the snapshot (first name, completeness) current."""
        key = f"auth:{hash_secret(token)}"
        raw = self._r().get(key)
        if not raw:
            return
        old = json.loads(raw)
        ttl = max(int(old.get("expires_at", 0) - time.time()), 1)
        self._r().setex(key, ttl, json.dumps(self._snapshot(acct, expires_at=old.get("expires_at"))))

    def session_account(self, token):
        """Identity only - never touches Postgres."""
        if not token:
            return None
        raw = self._r().get(f"auth:{hash_secret(token)}")
        if not raw:
            return None
        record = json.loads(raw)
        if record.get("expires_at", 0) <= time.time():
            self.delete_session(token)
            return None
        return Identity(customer_id=record.get("customer_id"), email=record["email"], name=record.get("name") or "",
                        first_name=record.get("first_name") or (record.get("name") or "").split(" ")[0],
                        profile_version=record.get("profile_version", 1),
                        profile_complete=bool(record.get("profile_complete", False)),
                        preferred_currency=record.get("preferred_currency", "USD"))

    def delete_session(self, token):
        if token:
            self._r().delete(f"auth:{hash_secret(token)}")
