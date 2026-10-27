"""Postgres access for accountservice: migrations, schema readiness, repository.

Design:
  * Migrations run on a dedicated NON-pooled connection inside ONE transaction guarded by
    pg_try_advisory_xact_lock. Each migration file declares `-- requires: NNNN`; the planner refuses
    out-of-order, skipped or unsatisfied-dependency application (plan_migrations is pure and tested).
  * Schema readiness is bound to accounts_meta.instance_id and invalidated on connection errors,
    missing-object errors, every new physical connection and a periodic check.
  * Every repository call is gated by SchemaManager.require_ready() and wrapped in a business span
    (accounts.db.*) under which the psycopg CLIENT spans nest. Spans carry opaque ids only: never
    names, email, phone, addresses, hashes or codes.
"""
import contextlib
import hashlib
import os
import random
import re
import threading
import time

import psycopg
from opentelemetry import trace
from psycopg import errors as pgerr
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

import config
import telemetry as tel
import telemetry_sanitize as san

tracer = trace.get_tracer("accountservice.accounts")
MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")
MIGRATION_LOCK_KEY = "online-boutique.accounts.migrations"
REAPER_LOCK_KEY = "online-boutique.accounts.reaper"

_SCHEMA_ERRORS = (pgerr.UndefinedTable, pgerr.InvalidSchemaName, pgerr.UndefinedFunction)
_REQUIRES_RE = re.compile(r"^--\s*requires:\s*(\d{4})\s*$", re.M)


class AccountStoreUnavailable(Exception):
    """reason: unreachable | timeout | schema_not_ready | schema_mismatch"""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class ChecksumMismatch(Exception):
    pass


class MigrationOrderError(Exception):
    """A migration is out of order, skipped, or its declared dependency is not satisfied."""


def migration_files(directory=None):
    """-> [(version, checksum, text, requires)] sorted by version."""
    directory = directory or MIGRATIONS_DIR
    out = []
    for name in sorted(os.listdir(directory)):
        if name.endswith(".sql"):
            with open(os.path.join(directory, name), encoding="utf-8") as f:
                text = f.read()
            header = "\n".join(text.splitlines()[:12])
            out.append((name.split("_", 1)[0], hashlib.sha256(text.encode()).hexdigest(), text,
                        tuple(_REQUIRES_RE.findall(header))))
    return out


def expected_version(directory=None):
    return migration_files(directory)[-1][0]


def plan_migrations(files, applied):
    """Pure planner. `applied` maps version -> checksum. Returns the pending list in apply order, or
    raises ChecksumMismatch / MigrationOrderError. Rules: applied versions must match their
    checksum; pending versions must be a contiguous suffix after the highest applied version; every
    declared `requires` must already be applied or be applied earlier in this same run."""
    pending, seen = [], set(applied)
    highest = max(applied) if applied else None
    for version, checksum, text, requires in files:
        if version in applied:
            if applied[version] != checksum:
                raise ChecksumMismatch(version)
            continue
        if highest is not None and version < highest:
            raise MigrationOrderError(f"{version} is older than applied {highest}")
        for dep in requires:
            if dep not in seen:
                raise MigrationOrderError(f"{version} requires {dep}, which is not applied")
        pending.append((version, checksum, text))
        seen.add(version)
    return pending


def _lock_sql():
    return "SELECT pg_try_advisory_xact_lock(hashtext(%s))"


# --- migrator --------------------------------------------------------------------------------
def run_migrations(dsn, directory=None):
    """One attempt. Returns (outcome, instance_id, version); outcome: applied | up_to_date | locked_elsewhere."""
    with tracer.start_as_current_span("accounts.schema.migrate") as span:
        try:
            result = _migrate_once(dsn, directory)
        except (ChecksumMismatch, MigrationOrderError) as e:
            tel.set_attrs(span, {"app.account.migration.outcome": "failed"})
            span.set_attribute("error.type", type(e).__name__)
            raise
        except Exception as e:
            tel.set_attrs(span, {"app.account.migration.outcome": "failed"})
            span.set_attribute("error.type", type(e).__name__)
            raise
        tel.set_attrs(span, {"app.account.migration.outcome": result[0]})
        if result[2]:
            tel.set_attrs(span, {"app.account.migration.version": result[2]})
        return result


def _migrate_once(dsn, directory=None):
    with psycopg.connect(dsn, autocommit=False, connect_timeout=3) as conn:
        conn.execute("SET LOCAL lock_timeout = '5s'")
        conn.execute("SET LOCAL statement_timeout = '60s'")
        if not conn.execute(_lock_sql(), (MIGRATION_LOCK_KEY,)).fetchone()[0]:
            conn.rollback()
            return "locked_elsewhere", None, None
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                     "version text PRIMARY KEY, checksum text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())")
        applied = dict(conn.execute("SELECT version, checksum FROM schema_migrations").fetchall())
        try:
            pending = plan_migrations(migration_files(directory), applied)
        except (ChecksumMismatch, MigrationOrderError):
            conn.rollback()
            raise
        for version, checksum, text in pending:
            conn.execute(text)
            conn.execute("INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)", (version, checksum))
        instance_id, version = conn.execute(
            "SELECT (SELECT instance_id::text FROM accounts_meta), (SELECT max(version) FROM schema_migrations)").fetchone()
        conn.commit()
        return ("applied" if pending else "up_to_date"), instance_id, version


# --- schema readiness --------------------------------------------------------------------------
class SchemaManager:
    """state: unknown | migrating | ready | stale | failed. `ready` is bound to an instance_id."""

    def __init__(self, dsn, pool_getter, retry_debounce_s=1.0, directory=None):
        self.dsn = dsn
        self._pool = pool_getter
        self.directory = directory
        self.state, self.instance_id, self.version, self.failure = "unknown", None, None, None
        self._mu = threading.Lock()
        self._attempt_lock = threading.Lock()
        self._last_attempt = 0.0
        self._debounce = retry_debounce_s
        self.validated_at = 0.0

    def mark_stale(self, why="error"):
        with self._mu:
            if self.state == "ready":
                self.state = "stale"
                tel.event("accounts.schema.stale")

    def require_ready(self):
        if self.state == "ready":
            return
        if self.state == "failed" and self.failure == "schema_mismatch":
            raise AccountStoreUnavailable("schema_mismatch")
        with tracer.start_as_current_span("accounts.schema.validate") as span:
            if self.state == "stale" and self._validate(span):
                return
            self._ensure(span)
        if self.state != "ready":
            raise AccountStoreUnavailable("schema_mismatch" if self.failure == "schema_mismatch" else "schema_not_ready")

    def _validate(self, span):
        try:
            with self._pool().connection() as conn:
                row = conn.execute("SELECT (SELECT instance_id::text FROM accounts_meta), "
                                   "(SELECT max(version) FROM schema_migrations)").fetchone()
        except _SCHEMA_ERRORS:
            self._set("migrating")
            tel.set_attrs(span, {"app.account.schema.validation": "missing_schema"})
            return False
        except (pgerr.QueryCanceled, PoolTimeout) as e:
            raise AccountStoreUnavailable("timeout") from e
        except (psycopg.OperationalError, psycopg.InterfaceError) as e:
            raise AccountStoreUnavailable("unreachable") from e
        instance_id, version = (row["instance_id"], row["max"]) if isinstance(row, dict) else row
        if instance_id is None or version is None or version < expected_version(self.directory):
            self._set("migrating")
            tel.set_attrs(span, {"app.account.schema.validation": "uninitialised"})
            return False
        if self.instance_id and instance_id != self.instance_id:
            tel.warn("accounts.db.replaced")
            tel.set_attrs(span, {"app.account.db.instance_changed": True})
            self._set("migrating")
            return False
        self._set("ready", instance_id=instance_id, version=version)
        return True

    def _ensure(self, span):
        now = time.monotonic()
        if now - self._last_attempt < self._debounce:
            return
        if not self._attempt_lock.acquire(blocking=False):
            return
        try:
            self._last_attempt = time.monotonic()
            self.attempt()
        finally:
            self._attempt_lock.release()

    def attempt(self):
        """Run the migrator once and update state. Never raises."""
        prev = self.state
        self._set("migrating")
        try:
            outcome, instance_id, version = run_migrations(self.dsn, self.directory)
        except (ChecksumMismatch, MigrationOrderError) as e:
            tel.warn("accounts.migration.failed", **{"error.type": type(e).__name__})
            self._set("failed", failure="schema_mismatch")
            return "failed"
        except Exception as e:  # noqa: BLE001
            tel.warn("accounts.migration.failed", **{"error.type": type(e).__name__})
            self._set("unknown" if prev in ("unknown", "migrating") else "stale", failure="schema_not_ready")
            return "failed"
        if outcome == "locked_elsewhere":
            self._set("unknown", failure="schema_not_ready")
            tel.event("accounts.migration.locked_elsewhere")
            return outcome
        if self.instance_id and instance_id != self.instance_id:
            tel.warn("accounts.db.replaced", **{"app.account.db.instance_changed": True})
        tel.event(f"accounts.migration.{outcome}", **{"app.account.migration.version": version})
        self._set("ready", instance_id=instance_id, version=version)
        return outcome

    def _set(self, state, instance_id=None, version=None, failure=None):
        with self._mu:
            self.state = state
            if state == "ready":
                self.instance_id, self.version, self.failure = instance_id, version, None
                self.validated_at = time.monotonic()
            elif failure:
                self.failure = failure


# --- repository --------------------------------------------------------------------------------
class _Op:
    def __init__(self, span, conn):
        self.span, self.conn, self.outcome = span, conn, "ok"


_CUSTOMER_COLS = (
    "c.id::text AS id, c.email, c.email_display, c.name, c.first_name, c.last_name, c.phone_e164, c.status, "
    "c.password_hash, c.preferred_currency, c.created_via, c.profile_version, "
    "extract(epoch FROM c.created_at)::bigint AS created_at, "
    "EXISTS (SELECT 1 FROM customer_addresses ca WHERE ca.customer_id = c.id) AS has_address")


class PgAccountRepository:
    def __init__(self, dsn=None, pool_max=None, retry_debounce_s=1.0, directory=None):
        self.dsn = dsn or config.ACCOUNTS_DATABASE_URL
        self._pool_max = pool_max or config.ACCOUNTS_DB_POOL_MAX
        self._pool = None
        self._pool_mu = threading.Lock()
        self.schema = SchemaManager(self.dsn, self._get_pool, retry_debounce_s, directory)
        self._last_failure = 0.0
        self._stop = threading.Event()
        self._thread = None

    # pool ------------------------------------------------------------------------------------
    def _get_pool(self):
        if self._pool is None:
            with self._pool_mu:
                if self._pool is None:
                    opts = (f"-c statement_timeout={config.ACCOUNTS_DB_STATEMENT_TIMEOUT_MS} "
                            "-c lock_timeout=1000 -c idle_in_transaction_session_timeout=10000")
                    pool = ConnectionPool(
                        self.dsn, min_size=0, max_size=self._pool_max, timeout=2.0, open=False,
                        kwargs={"row_factory": dict_row, "options": opts, "connect_timeout": 2},
                        check=ConnectionPool.check_connection,
                        configure=lambda conn: self.schema.mark_stale("new_connection"))
                    pool.open(wait=False)
                    self._pool = pool
        return self._pool

    def close(self):
        self._stop.set()
        if self._pool:
            self._pool.close()

    def healthy(self):
        return self.schema.state == "ready" and time.monotonic() - self._last_failure > 5

    # background migrator / reaper ------------------------------------------------------------
    def start(self):
        if self._thread:
            return
        self._thread = threading.Thread(target=self._loop, name="accounts-migrator", daemon=True)
        self._thread.start()

    def _loop(self):
        delay, last_check, last_reap = 1.0, time.monotonic(), time.monotonic()
        while not self._stop.is_set():
            if self.schema.state != "ready":
                with self.schema._attempt_lock:
                    self.schema.attempt()
                delay = 1.0 if self.schema.state == "ready" else min(delay * 2, config.ACCOUNTS_MIGRATION_RETRY_MAX_S)
                self._stop.wait(delay * random.uniform(0.8, 1.2))
                continue
            now = time.monotonic()
            if now - last_check > 60:
                last_check = now
                self.schema.mark_stale("periodic")
            if now - last_reap > 600:
                last_reap = now
                with contextlib.suppress(Exception):
                    self.reap()
            self._stop.wait(5)

    # operation wrapper -----------------------------------------------------------------------
    @contextlib.contextmanager
    def _op(self, name, customer_id=None):
        t0 = time.monotonic()
        with tracer.start_as_current_span(f"accounts.db.{name}") as span:
            tel.stamp_app_attrs(span)
            if customer_id:
                tel.set_attrs(span, {"app.customer.id": customer_id})
            op = _Op(span, None)
            try:
                self.schema.require_ready()
                with self._get_pool().connection() as conn:
                    if self.schema.state != "ready":
                        self.schema.require_ready()
                    op.conn = conn
                    yield op
            except AccountStoreUnavailable as e:
                self._failed(op, span, e.reason)
                raise
            except pgerr.QueryCanceled as e:
                self._failed(op, span, "timeout", e)
                raise AccountStoreUnavailable("timeout") from e
            except PoolTimeout as e:
                self._failed(op, span, "timeout", e)
                raise AccountStoreUnavailable("timeout") from e
            except _SCHEMA_ERRORS as e:
                self.schema.mark_stale("missing_object")
                self._failed(op, span, "schema_not_ready", e)
                raise AccountStoreUnavailable("schema_not_ready") from e
            except (psycopg.OperationalError, psycopg.InterfaceError) as e:
                self.schema.mark_stale("connection_error")
                self._failed(op, span, "unreachable", e)
                raise AccountStoreUnavailable("unreachable") from e
            finally:
                tel.set_attrs(span, {"app.account.db.outcome": op.outcome})
                tel.account_db_duration.record(time.monotonic() - t0, {"operation": name, "outcome": op.outcome})

    def _failed(self, op, span, reason, exc=None):
        self._last_failure = time.monotonic()
        op.outcome = reason
        if exc is not None:
            tel.record_exception(span, exc)
        tel.warn("accounts.db.unavailable", **{"app.account.db.outcome": reason})

    @staticmethod
    def _register(row):
        if row:
            san.register_person(row.get("first_name"), row.get("last_name"), row.get("email_display") or row.get("email"),
                                row.get("phone_e164"))
        return row

    # customers -------------------------------------------------------------------------------
    def get_customer_by_email(self, key):
        with self._op("customer.lookup") as op:
            row = self._register(op.conn.execute(f"SELECT {_CUSTOMER_COLS} FROM customers c WHERE c.email = %s", (key,)).fetchone())
            if row is None:
                op.outcome = "not_found"
            else:
                tel.set_attrs(op.span, {"app.customer.id": row["id"], "app.account.profile_version": row["profile_version"]})
            return row

    def get_customer(self, customer_id):
        with self._op("customer.get", customer_id) as op:
            row = self._register(op.conn.execute(f"SELECT {_CUSTOMER_COLS} FROM customers c WHERE c.id = %s", (customer_id,)).fetchone())
            if row is None:
                op.outcome = "not_found"
            return row

    @staticmethod
    def _insert_customer(conn, profile, status, via, password_hash):
        row = conn.execute(
            "INSERT INTO customers (email, email_display, name, first_name, last_name, phone_e164, status, created_via, "
            "password_hash, preferred_currency, profile_version, profile_completed_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 2, now()) ON CONFLICT (email) DO NOTHING RETURNING id::text AS id",
            (profile["email"], profile["email_display"], profile["name"], profile["first_name"], profile["last_name"],
             profile["phone_e164"], status, via, password_hash, profile.get("preferred_currency", "USD"))).fetchone()
        if row and profile.get("address"):
            PgAccountRepository._insert_address(conn, row["id"], profile["address"], make_default=True)
            PgAccountRepository._legacy_address(conn, row["id"], profile["address"])
        return row["id"] if row else None

    @staticmethod
    def _insert_address(conn, customer_id, a, make_default=False, label=None):
        if make_default:
            conn.execute("UPDATE customer_addresses SET is_default = false, updated_at = now() "
                         "WHERE customer_id = %s AND is_default", (customer_id,))
        row = conn.execute(
            "INSERT INTO customer_addresses (customer_id, label, line1, line2, city, state, zip, country, is_po_box, is_default) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, 'US', %s, %s) RETURNING id::text AS id",
            (customer_id, label, a["line1"], a.get("line2"), a["city"], a["state"], a["zip"], bool(a.get("is_po_box")),
             make_default)).fetchone()
        return row["id"]

    @staticmethod
    def _legacy_address(conn, customer_id, a):
        """Keep the pre-existing single-row table current so ACCOUNT_BACKEND=local readers keep working."""
        street = a["line1"] + (f", {a['line2']}" if a.get("line2") else "")
        conn.execute(
            "INSERT INTO addresses (customer_id, street_address, city, state, zip_code, country) "
            "VALUES (%s, %s, %s, %s, %s, 'US') ON CONFLICT (customer_id) DO UPDATE SET "
            "street_address = EXCLUDED.street_address, city = EXCLUDED.city, state = EXCLUDED.state, "
            "zip_code = EXCLUDED.zip_code, country = EXCLUDED.country, updated_at = now()",
            (customer_id, street, a["city"], a["state"], a["zip"]))

    _LOCK_ROW = (
        "SELECT c.id::text AS id, c.status, a.session_id_hash, a.used_at IS NOT NULL AS used, "
        "coalesce(a.expires_at > now(), false) AS live "
        "FROM customers c LEFT JOIN activation_claims a ON a.customer_id = c.id "
        "WHERE c.email = %s FOR UPDATE OF c")

    def register_web(self, profile, password_hash):
        """-> (outcome, customer_id). outcome: created | exists."""
        with self._op("customer.create") as op:
            for _ in range(3):
                cid = self._insert_customer(op.conn, profile, "active", "web", password_hash)
                if cid:
                    tel.set_attrs(op.span, {"app.customer.id": cid, "app.account.created_via": "web",
                                            "app.account.profile_version": 2})
                    return "created", cid
                row = op.conn.execute(self._LOCK_ROW, (profile["email"],)).fetchone()
                if row is None:
                    continue
                if row["status"] == "pending_activation" and not (row["live"] and not row["used"]):
                    op.conn.execute("DELETE FROM customers WHERE id = %s AND status = 'pending_activation'", (row["id"],))
                    continue
                op.outcome = "conflict"
                return "exists", None
            op.outcome = "conflict"
            return "exists", None

    def create_pending(self, profile, session_hash, code_hash):
        """Assistant sign-up. -> (outcome, customer_id): created | resumed | recreated_abandoned | pending_elsewhere | exists."""
        with self._op("claim.create") as op:
            for _ in range(3):
                row = op.conn.execute(self._LOCK_ROW, (profile["email"],)).fetchone()
                outcome = "created"
                if row:
                    if row["status"] == "active":
                        op.outcome = "conflict"
                        return "exists", row["id"]
                    live = row["live"] and not row["used"]
                    if live and row["session_id_hash"] == session_hash:
                        op.conn.execute(
                            "UPDATE activation_claims SET code_hash = %s, expires_at = now() + make_interval(secs => %s), "
                            "rotations = rotations + 1 WHERE customer_id = %s", (code_hash, config.CLAIM_TTL_SECONDS, row["id"]))
                        op.conn.execute(
                            "UPDATE customers SET name = %s, first_name = %s, last_name = %s, phone_e164 = %s, "
                            "email_display = %s, updated_at = now() WHERE id = %s",
                            (profile["name"], profile["first_name"], profile["last_name"], profile["phone_e164"],
                             profile["email_display"], row["id"]))
                        op.conn.execute("DELETE FROM customer_addresses WHERE customer_id = %s", (row["id"],))
                        self._insert_address(op.conn, row["id"], profile["address"], make_default=True)
                        self._legacy_address(op.conn, row["id"], profile["address"])
                        tel.set_attrs(op.span, {"app.customer.id": row["id"], "app.account.activation.outcome": "resumed"})
                        return "resumed", row["id"]
                    if live:
                        op.outcome = "conflict"
                        tel.set_attrs(op.span, {"app.account.activation.outcome": "pending_elsewhere"})
                        return "pending_elsewhere", row["id"]
                    op.conn.execute("DELETE FROM customers WHERE id = %s AND status = 'pending_activation'", (row["id"],))
                    outcome = "recreated_abandoned"
                cid = self._insert_customer(op.conn, profile, "pending_activation", "assistant", None)
                if cid is None:
                    continue
                op.conn.execute(
                    "INSERT INTO activation_claims (customer_id, session_id_hash, code_hash, expires_at) "
                    "VALUES (%s, %s, %s, now() + make_interval(secs => %s))",
                    (cid, session_hash, code_hash, config.CLAIM_TTL_SECONDS))
                tel.set_attrs(op.span, {"app.customer.id": cid, "app.account.activation.outcome": outcome,
                                        "app.account.created_via": "assistant", "app.account.profile_version": 2})
                return outcome, cid
            op.outcome = "conflict"
            return "exists", None

    def claim_is_live(self, customer_id, session_hash):
        with self._op("claim.validate", customer_id) as op:
            row = op.conn.execute(
                "SELECT c.email FROM activation_claims a JOIN customers c ON c.id = a.customer_id "
                "WHERE a.customer_id = %s AND a.session_id_hash = %s AND a.used_at IS NULL "
                "AND a.expires_at > now() AND c.status = 'pending_activation'", (customer_id, session_hash)).fetchone()
            if row is None:
                op.outcome = "not_found"
                return None
            return row["email"]

    def regenerate_claim(self, session_hash, code_hash):
        with self._op("claim.regenerate") as op:
            row = op.conn.execute(
                "UPDATE activation_claims a SET code_hash = %s, expires_at = now() + make_interval(secs => %s), "
                "rotations = rotations + 1 FROM customers c WHERE c.id = a.customer_id AND c.status = 'pending_activation' "
                "AND a.session_id_hash = %s AND a.used_at IS NULL AND a.expires_at > now() "
                "RETURNING c.id::text AS id, c.email", (code_hash, config.CLAIM_TTL_SECONDS, session_hash)).fetchone()
            if row is None:
                op.outcome = "not_found"
                return None
            tel.set_attrs(op.span, {"app.customer.id": row["id"], "app.account.activation.outcome": "regenerated"})
            return row

    def consume_claim(self, customer_id, session_hash, code_hash, password_hash):
        with self._op("claim.consume", customer_id) as op:
            ok = op.conn.execute(
                "UPDATE activation_claims SET used_at = now() WHERE customer_id = %s AND session_id_hash = %s "
                "AND code_hash = %s AND used_at IS NULL AND expires_at > now() RETURNING customer_id::text AS id",
                (customer_id, session_hash, code_hash)).fetchone()
            if ok is None:
                op.outcome = "conflict"
                tel.set_attrs(op.span, {"app.account.activation.outcome": "failed"})
                return None
            row = op.conn.execute(
                "UPDATE customers SET password_hash = %s, status = 'active', updated_at = now() "
                "WHERE id = %s AND status = 'pending_activation' RETURNING id::text AS id", (password_hash, customer_id)).fetchone()
            if row is None:
                op.conn.rollback()
                op.outcome = "conflict"
                return None
            op.conn.execute("INSERT INTO login_events (customer_id, outcome) VALUES (%s, 'activated')", (customer_id,))
            tel.set_attrs(op.span, {"app.account.activation.outcome": "consumed"})
            return self._register(op.conn.execute(f"SELECT {_CUSTOMER_COLS} FROM customers c WHERE c.id = %s", (customer_id,)).fetchone())

    def update_profile(self, customer_id, changes):
        """changes: first_name/last_name/phone_e164/preferred_currency. Upgrades to profile_version 2
        when the customer then has every required field. -> customer row | None."""
        with self._op("customer.update", customer_id) as op:
            cur = op.conn.execute("SELECT first_name, last_name, phone_e164, preferred_currency, email_display, email, name "
                                  "FROM customers WHERE id = %s FOR UPDATE", (customer_id,)).fetchone()
            if cur is None:
                op.outcome = "not_found"
                return None
            first = changes.get("first_name", cur["first_name"])
            last = changes.get("last_name", cur["last_name"])
            phone = changes.get("phone_e164", cur["phone_e164"])
            currency = changes.get("preferred_currency", cur["preferred_currency"])
            name = f"{first or ''} {last or ''}".strip() or cur["name"]
            has_addr = op.conn.execute("SELECT EXISTS (SELECT 1 FROM customer_addresses WHERE customer_id = %s) AS x", (customer_id,)).fetchone()["x"]
            complete = bool(first and last and phone and has_addr)
            op.conn.execute(
                "UPDATE customers SET first_name = %s, last_name = %s, phone_e164 = %s, preferred_currency = %s, name = %s, "
                "email_display = coalesce(email_display, email), "
                "profile_version = CASE WHEN %s THEN 2 ELSE profile_version END, "
                "profile_completed_at = CASE WHEN %s AND profile_completed_at IS NULL THEN now() ELSE profile_completed_at END, "
                "updated_at = now() WHERE id = %s", (first, last, phone, currency, name, complete, complete, customer_id))
            row = self._register(op.conn.execute(f"SELECT {_CUSTOMER_COLS} FROM customers c WHERE c.id = %s", (customer_id,)).fetchone())
            tel.set_attrs(op.span, {"app.account.profile_version": row["profile_version"]})
            return row

    def record_login_event(self, customer_id, outcome):
        with self._op("login_event", customer_id) as op:
            op.conn.execute("INSERT INTO login_events (customer_id, outcome) VALUES (%s, %s)", (customer_id, outcome))

    # addresses -------------------------------------------------------------------------------
    _ADDRESS_COLS = ("id::text AS id, label, line1, line2, city, state, zip, country, is_po_box, is_default, "
                     "address_verification_status")

    def list_addresses(self, customer_id):
        with self._op("address.list", customer_id) as op:
            rows = op.conn.execute(f"SELECT {self._ADDRESS_COLS} FROM customer_addresses WHERE customer_id = %s "
                                   "ORDER BY is_default DESC, created_at", (customer_id,)).fetchall()
            for r in rows:
                san.register_address(r)
            tel.set_attrs(op.span, {"app.account.address_count": len(rows)})
            return rows

    def create_address(self, customer_id, a, label=None, make_default=False):
        """-> (outcome, row). outcome: created | limit_reached."""
        with self._op("address.create", customer_id) as op:
            op.conn.execute("SELECT id FROM customers WHERE id = %s FOR UPDATE", (customer_id,))
            n = op.conn.execute("SELECT count(*) AS n FROM customer_addresses WHERE customer_id = %s", (customer_id,)).fetchone()["n"]
            if n >= config.MAX_ADDRESSES:
                op.outcome = "conflict"
                return "limit_reached", None
            default = make_default or n == 0
            aid = self._insert_address(op.conn, customer_id, a, make_default=default, label=label)
            if default:
                self._legacy_address(op.conn, customer_id, a)
            row = op.conn.execute(f"SELECT {self._ADDRESS_COLS} FROM customer_addresses WHERE id = %s", (aid,)).fetchone()
            san.register_address(row)
            tel.set_attrs(op.span, {"app.account.address_count": n + 1})
            return "created", row

    def update_address(self, customer_id, address_id, a, label=None):
        with self._op("address.update", customer_id) as op:
            row = op.conn.execute(
                "UPDATE customer_addresses SET label = %s, line1 = %s, line2 = %s, city = %s, state = %s, zip = %s, "
                "is_po_box = %s, address_verification_status = 'unverified', updated_at = now() "
                f"WHERE id = %s AND customer_id = %s RETURNING {self._ADDRESS_COLS}",
                (label, a["line1"], a.get("line2"), a["city"], a["state"], a["zip"], bool(a.get("is_po_box")),
                 address_id, customer_id)).fetchone()
            if row is None:
                op.outcome = "not_found"
                return None
            if row["is_default"]:
                self._legacy_address(op.conn, customer_id, a)
            san.register_address(row)
            return row

    def delete_address(self, customer_id, address_id):
        with self._op("address.delete", customer_id) as op:
            row = op.conn.execute("DELETE FROM customer_addresses WHERE id = %s AND customer_id = %s RETURNING is_default",
                                  (address_id, customer_id)).fetchone()
            if row is None:
                op.outcome = "not_found"
                return False
            if row["is_default"]:
                op.conn.execute("UPDATE customer_addresses SET is_default = true, updated_at = now() WHERE id = ("
                                "SELECT id FROM customer_addresses WHERE customer_id = %s ORDER BY created_at LIMIT 1)", (customer_id,))
            return True

    def set_default_address(self, customer_id, address_id):
        with self._op("address.default", customer_id) as op:
            op.conn.execute("SELECT id FROM customers WHERE id = %s FOR UPDATE", (customer_id,))
            exists = op.conn.execute("SELECT 1 FROM customer_addresses WHERE id = %s AND customer_id = %s", (address_id, customer_id)).fetchone()
            if exists is None:
                op.outcome = "not_found"
                return None
            op.conn.execute("UPDATE customer_addresses SET is_default = false, updated_at = now() WHERE customer_id = %s AND is_default", (customer_id,))
            row = op.conn.execute(f"UPDATE customer_addresses SET is_default = true, updated_at = now() WHERE id = %s RETURNING {self._ADDRESS_COLS}",
                                  (address_id,)).fetchone()
            self._legacy_address(op.conn, customer_id, row | {"line1": row["line1"]})
            return row

    # --- payment methods: opaque demo tokens + safe metadata only (no PAN/CVV ever reaches here) ----
    _PM_COLS = "id::text AS id, catalog_id, brand, last4, exp_month, exp_year, label, is_default, created_at"

    def list_payment_methods(self, customer_id):
        with self._op("payment.list", customer_id) as op:
            rows = op.conn.execute(f"SELECT {self._PM_COLS} FROM payment_methods WHERE customer_id = %s AND deleted_at IS NULL "
                                   "ORDER BY is_default DESC, created_at", (customer_id,)).fetchall()
            tel.set_attrs(op.span, {"app.account.payment_method_count": len(rows)})
            return rows

    def add_payment_method(self, customer_id, token, card, label=None, make_default=False):
        """-> (outcome, row). outcome: created | limit_reached. `card` is catalog metadata from Tokenize."""
        with self._op("payment.add", customer_id) as op:
            op.conn.execute("SELECT id FROM customers WHERE id = %s FOR UPDATE", (customer_id,))
            n = op.conn.execute("SELECT count(*) AS n FROM payment_methods WHERE customer_id = %s AND deleted_at IS NULL",
                                (customer_id,)).fetchone()["n"]
            if n >= config.MAX_PAYMENT_METHODS:
                op.outcome = "conflict"
                return "limit_reached", None
            default = make_default or n == 0
            if default:
                op.conn.execute("UPDATE payment_methods SET is_default = false, updated_at = now() "
                                "WHERE customer_id = %s AND is_default AND deleted_at IS NULL", (customer_id,))
            row = op.conn.execute(
                "INSERT INTO payment_methods (customer_id, provider_token, catalog_id, brand, last4, exp_month, exp_year, label, is_default) "
                f"VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING {self._PM_COLS}",
                (customer_id, token, card["catalog_id"], card["brand"], card["last4"], card["exp_month"], card["exp_year"],
                 label, default)).fetchone()
            tel.set_attrs(op.span, {"app.account.payment_method_count": n + 1})
            return "created", row

    def delete_payment_method(self, customer_id, pm_id):
        """Soft delete (orders keep their reference); the token is revoked so it can never be charged again."""
        with self._op("payment.delete", customer_id) as op:
            row = op.conn.execute(
                "UPDATE payment_methods SET deleted_at = now(), is_default = false, provider_token = 'revoked:' || id::text, "
                "updated_at = now() WHERE id = %s AND customer_id = %s AND deleted_at IS NULL "
                "RETURNING (SELECT is_default FROM payment_methods p WHERE p.id = payment_methods.id) AS was_default",
                (pm_id, customer_id)).fetchone()
            if row is None:
                op.outcome = "not_found"
                return False
            if row["was_default"]:
                op.conn.execute("UPDATE payment_methods SET is_default = true, updated_at = now() WHERE id = ("
                                "SELECT id FROM payment_methods WHERE customer_id = %s AND deleted_at IS NULL "
                                "ORDER BY created_at LIMIT 1)", (customer_id,))
            return True

    def set_default_payment_method(self, customer_id, pm_id):
        with self._op("payment.default", customer_id) as op:
            op.conn.execute("SELECT id FROM customers WHERE id = %s FOR UPDATE", (customer_id,))
            exists = op.conn.execute("SELECT 1 FROM payment_methods WHERE id = %s AND customer_id = %s AND deleted_at IS NULL",
                                     (pm_id, customer_id)).fetchone()
            if exists is None:
                op.outcome = "not_found"
                return None
            op.conn.execute("UPDATE payment_methods SET is_default = false, updated_at = now() "
                            "WHERE customer_id = %s AND is_default", (customer_id,))
            return op.conn.execute(f"UPDATE payment_methods SET is_default = true, updated_at = now() WHERE id = %s "
                                   f"RETURNING {self._PM_COLS}", (pm_id,)).fetchone()

    def resolve_payment_token(self, customer_id, pm_id):
        """Service-only: the charge token for one of THIS customer's active methods, else None."""
        with self._op("payment.resolve", customer_id) as op:
            row = op.conn.execute("SELECT provider_token, brand, last4 FROM payment_methods "
                                  "WHERE id = %s AND customer_id = %s AND deleted_at IS NULL", (pm_id, customer_id)).fetchone()
            if row is None:
                op.outcome = "not_found"
            return row

    def reap(self):
        """Delete pending accounts whose claim expired > 15 min ago (single replica at a time)."""
        with self._op("reap") as op:
            if not op.conn.execute(_lock_sql(), (REAPER_LOCK_KEY,)).fetchone()["pg_try_advisory_xact_lock"]:
                return 0
            n = op.conn.execute(
                "DELETE FROM customers c USING activation_claims a WHERE a.customer_id = c.id "
                "AND c.status = 'pending_activation' AND a.used_at IS NULL "
                "AND a.expires_at < now() - interval '15 minutes'").rowcount
            if n:
                tel.account_pending_reaped.add(n)
            return n
