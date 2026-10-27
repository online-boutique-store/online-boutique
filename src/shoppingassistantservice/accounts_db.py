"""Postgres access for customer accounts: migrations, schema readiness, repository.

Design (see the accounts-postgres-otel plan):
  * Migrations run on a dedicated NON-pooled connection inside ONE transaction guarded by
    pg_try_advisory_xact_lock, so the lock cannot leak into the pool and a failed attempt
    leaves nothing behind.
  * Schema readiness is bound to accounts_meta.instance_id and is invalidated on connection
    errors, missing-object errors, every new physical connection and a periodic check, so a
    replaced / re-initialised database is never used before it is revalidated.
  * Every repository call is gated by SchemaManager.require_ready() and wrapped in a business
    span (accounts.db.*) under which the psycopg CLIENT spans nest.
Nothing here ever puts passwords, hashes, activation codes or addresses into spans or logs.
"""
import contextlib
import hashlib
import os
import random
import threading
import time

import psycopg
from opentelemetry import trace
from psycopg import errors as pgerr
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

import config
import telemetry as tel
import telemetry_sanitize

tracer = trace.get_tracer("shoppingassistantservice.accounts")
MIGRATIONS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrations")
MIGRATION_LOCK_KEY = "online-boutique.accounts.migrations"
REAPER_LOCK_KEY = "online-boutique.accounts.reaper"
CLAIM_TTL_SECONDS = 900

_SCHEMA_ERRORS = (pgerr.UndefinedTable, pgerr.InvalidSchemaName, pgerr.UndefinedFunction)


class AccountStoreUnavailable(Exception):
    """reason: unreachable | timeout | schema_not_ready | schema_mismatch"""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class ChecksumMismatch(Exception):
    pass


def migration_files():
    out = []
    for name in sorted(os.listdir(MIGRATIONS_DIR)):
        if name.endswith(".sql"):
            with open(os.path.join(MIGRATIONS_DIR, name), encoding="utf-8") as f:
                text = f.read()
            out.append((name.split("_", 1)[0], hashlib.sha256(text.encode()).hexdigest(), text))
    return out


def expected_version():
    return migration_files()[-1][0]


def _lock_sql():
    return "SELECT pg_try_advisory_xact_lock(hashtext(%s))"


# --- migrator -----------------------------------------------------------------------------
def run_migrations(dsn):
    """One attempt. Returns (outcome, instance_id, version); outcome is applied | up_to_date |
    locked_elsewhere. Raises ChecksumMismatch or psycopg errors (state stays unchanged)."""
    with tracer.start_as_current_span("accounts.schema.migrate") as span:
        try:
            result = _migrate_once(dsn)
        except ChecksumMismatch:
            span.set_attribute("app.account.migration.outcome", "failed")
            span.set_attribute("error.type", "ChecksumMismatch")
            raise
        except Exception as e:
            span.set_attribute("app.account.migration.outcome", "failed")
            span.set_attribute("error.type", type(e).__name__)
            raise
        span.set_attribute("app.account.migration.outcome", result[0])
        if result[2]:
            span.set_attribute("app.account.migration.version", result[2])
        return result


def _migrate_once(dsn):
    # Dedicated connection: migration session state can never enter the pool.
    with psycopg.connect(dsn, autocommit=False, connect_timeout=3) as conn:
        conn.execute("SET LOCAL lock_timeout = '5s'")
        conn.execute("SET LOCAL statement_timeout = '60s'")
        if not conn.execute(_lock_sql(), (MIGRATION_LOCK_KEY,)).fetchone()[0]:
            conn.rollback()
            return "locked_elsewhere", None, None
        conn.execute("CREATE TABLE IF NOT EXISTS schema_migrations ("
                     "version text PRIMARY KEY, checksum text NOT NULL, applied_at timestamptz NOT NULL DEFAULT now())")
        applied = dict(conn.execute("SELECT version, checksum FROM schema_migrations").fetchall())
        pending = []
        for version, checksum, text in migration_files():
            if version in applied:
                if applied[version] != checksum:
                    conn.rollback()
                    raise ChecksumMismatch(version)
            else:
                pending.append((version, checksum, text))
        for version, checksum, text in pending:
            conn.execute(text)
            conn.execute("INSERT INTO schema_migrations (version, checksum) VALUES (%s, %s)", (version, checksum))
        instance_id, version = conn.execute(
            "SELECT (SELECT instance_id::text FROM accounts_meta), (SELECT max(version) FROM schema_migrations)").fetchone()
        conn.commit()  # releases the advisory xact lock
        return ("applied" if pending else "up_to_date"), instance_id, version


# --- schema readiness -------------------------------------------------------------------------
class SchemaManager:
    """state: unknown | migrating | ready | stale | failed. `ready` is bound to an instance_id."""

    def __init__(self, dsn, pool_getter, retry_debounce_s=1.0):
        self.dsn = dsn
        self._pool = pool_getter
        self.state, self.instance_id, self.version, self.failure = "unknown", None, None, None
        self._mu = threading.Lock()
        self._attempt_lock = threading.Lock()
        self._last_attempt = 0.0
        self._debounce = retry_debounce_s
        self.validated_at = 0.0

    # invalidation ---------------------------------------------------------------------------
    def mark_stale(self, why="error"):
        with self._mu:
            if self.state == "ready":
                self.state = "stale"
                tel.event("accounts.schema.stale", reason=why)

    # gating ---------------------------------------------------------------------------------
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
        """Read-only check against the CURRENT database. True only if tables, version and
        instance id are all as expected."""
        try:
            with self._pool().connection() as conn:
                row = conn.execute("SELECT (SELECT instance_id::text FROM accounts_meta), "
                                   "(SELECT max(version) FROM schema_migrations)").fetchone()
        except _SCHEMA_ERRORS:
            self._set("migrating")
            span.set_attribute("app.account.schema.validation", "missing_schema")
            return False
        except pgerr.QueryCanceled as e:
            raise AccountStoreUnavailable("timeout") from e
        except PoolTimeout as e:
            raise AccountStoreUnavailable("timeout") from e
        except (psycopg.OperationalError, psycopg.InterfaceError) as e:
            raise AccountStoreUnavailable("unreachable") from e
        instance_id, version = (row["instance_id"], row["max"]) if isinstance(row, dict) else row
        if instance_id is None or version is None or version < expected_version():
            self._set("migrating")
            span.set_attribute("app.account.schema.validation", "uninitialised")
            return False
        if self.instance_id and instance_id != self.instance_id:
            # Replaced / restored database: route through the migrator (verifies checksums).
            tel.warn("accounts.db.replaced")
            span.set_attribute("app.account.db.instance_changed", True)
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
        """Run the migrator once and update state. Never raises (callers re-check state)."""
        prev = self.state
        self._set("migrating")
        try:
            outcome, instance_id, version = run_migrations(self.dsn)
        except ChecksumMismatch:
            tel.warn("accounts.migration.failed", **{"error.type": "ChecksumMismatch"})
            self._set("failed", failure="schema_mismatch")
            return "failed"
        except Exception as e:  # noqa: BLE001 - unreachable DB, timeouts, bad SQL...
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


# --- repository -------------------------------------------------------------------------------
class _Op:
    def __init__(self, span, conn):
        self.span, self.conn, self.outcome = span, conn, "ok"


class PgAccountRepository:
    def __init__(self, dsn=None, pool_max=None, retry_debounce_s=1.0):
        self.dsn = dsn or config.ACCOUNTS_DATABASE_URL
        self._pool_max = pool_max or config.ACCOUNTS_DB_POOL_MAX
        self._pool = None
        self._pool_mu = threading.Lock()
        self.schema = SchemaManager(self.dsn, self._get_pool, retry_debounce_s)
        self._last_failure = 0.0
        self._stop = threading.Event()
        self._thread = None

    # pool -----------------------------------------------------------------------------------
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

    def pool_stats(self):
        return self._pool.get_stats() if self._pool else {}

    def close(self):
        self._stop.set()
        if self._pool:
            self._pool.close()

    def healthy(self):
        """Cheap, no query: schema usable and no recent failure."""
        return self.schema.state == "ready" and time.monotonic() - self._last_failure > 5

    # background migrator / reaper -------------------------------------------------------------
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
                if self.schema.state == "ready":
                    delay = 1.0
                else:
                    delay = min(delay * 2, config.ACCOUNTS_MIGRATION_RETRY_MAX_S)
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

    # operation wrapper ----------------------------------------------------------------------
    @contextlib.contextmanager
    def _op(self, name, **attrs):
        t0 = time.monotonic()
        with tracer.start_as_current_span(f"accounts.db.{name}") as span:
            tel.stamp_app_attrs(span)
            for k, v in attrs.items():
                if v:
                    span.set_attribute(k, v)
            op = _Op(span, None)
            try:
                self.schema.require_ready()
                with self._get_pool().connection() as conn:
                    if self.schema.state != "ready":  # a new physical connection invalidated readiness
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
                span.set_attribute("app.account.db.outcome", op.outcome)
                tel.account_db_duration.record(time.monotonic() - t0, {"operation": name, "outcome": op.outcome})

    def _failed(self, op, span, reason, exc=None):
        self._last_failure = time.monotonic()
        op.outcome = reason
        if exc is not None:
            span.set_attribute("error.type", type(exc).__name__)
            span.set_status(trace.Status(trace.StatusCode.ERROR, type(exc).__name__))
        tel.warn("accounts.db.unavailable", **{"app.account.db.outcome": reason})

    # queries --------------------------------------------------------------------------------
    _CUSTOMER_SQL = (
        "SELECT c.id::text AS id, c.email, c.name, c.status, c.password_hash, c.preferred_currency, "
        "c.default_payment_method_id, extract(epoch FROM c.created_at)::bigint AS created_at, "
        "a.street_address, a.city, a.state, a.zip_code, a.country "
        "FROM customers c LEFT JOIN addresses a ON a.customer_id = c.id WHERE c.email = %s")

    @staticmethod
    def _shape(row):
        if not row:
            return None
        addr = {k: row.pop(k) for k in ("street_address", "city", "state", "zip_code", "country")}
        row["address"] = addr if addr["street_address"] else {}
        telemetry_sanitize.register_address(addr)
        return row

    def get_customer(self, email):
        with self._op("customer.lookup", **{"app.customer.email": email}) as op:
            row = self._shape(op.conn.execute(self._CUSTOMER_SQL, (email,)).fetchone())
            if row is None:
                op.outcome = "not_found"
            else:
                op.span.set_attribute("app.customer.id", row["id"])
            return row

    @staticmethod
    def _insert_customer(conn, email, name, status, via, password_hash, address, currency="USD"):
        row = conn.execute(
            "INSERT INTO customers (email, name, status, created_via, password_hash, preferred_currency) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (email) DO NOTHING RETURNING id::text AS id",
            (email, name, status, via, password_hash, currency)).fetchone()
        if row and address:
            PgAccountRepository._upsert_address(conn, row["id"], address)
        return row["id"] if row else None

    @staticmethod
    def _upsert_address(conn, customer_id, a):
        conn.execute(
            "INSERT INTO addresses (customer_id, street_address, city, state, zip_code, country) "
            "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (customer_id) DO UPDATE SET "
            "street_address = EXCLUDED.street_address, city = EXCLUDED.city, state = EXCLUDED.state, "
            "zip_code = EXCLUDED.zip_code, country = EXCLUDED.country, updated_at = now()",
            (customer_id, a["street_address"], a["city"], a["state"], a["zip_code"], a["country"]))

    @staticmethod
    def _new_claim(conn, customer_id, session_hash, code_hash):
        conn.execute(
            "INSERT INTO activation_claims (customer_id, session_id_hash, code_hash, expires_at) "
            "VALUES (%s, %s, %s, now() + make_interval(secs => %s))",
            (customer_id, session_hash, code_hash, CLAIM_TTL_SECONDS))

    _LOCK_ROW = (
        "SELECT c.id::text AS id, c.status, a.session_id_hash, a.used_at IS NOT NULL AS used, "
        "coalesce(a.expires_at > now(), false) AS live "
        "FROM customers c LEFT JOIN activation_claims a ON a.customer_id = c.id "
        "WHERE c.email = %s FOR UPDATE OF c")

    def create_pending(self, email, name, address, session_hash, code_hash):
        """Chat account creation (protocol 4.1). -> (outcome, customer_id). outcome: created |
        resumed | recreated_abandoned | pending_elsewhere | exists."""
        with self._op("claim.create", **{"app.customer.email": email, "app.customer.name": name}) as op:
            for _ in range(3):
                row = op.conn.execute(self._LOCK_ROW, (email,)).fetchone()
                outcome = "created"
                if row:
                    if row["status"] == "active":
                        op.outcome = "conflict"
                        return "exists", row["id"]
                    live = row["live"] and not row["used"]
                    if live and row["session_id_hash"] == session_hash:
                        op.conn.execute(
                            "UPDATE activation_claims SET code_hash = %s, expires_at = now() + make_interval(secs => %s), "
                            "rotations = rotations + 1 WHERE customer_id = %s", (code_hash, CLAIM_TTL_SECONDS, row["id"]))
                        op.conn.execute("UPDATE customers SET name = %s, updated_at = now() WHERE id = %s", (name, row["id"]))
                        self._upsert_address(op.conn, row["id"], address)
                        op.span.set_attribute("app.customer.id", row["id"])
                        op.span.set_attribute("app.account.activation.outcome", "resumed")
                        return "resumed", row["id"]
                    if live:
                        op.outcome = "conflict"
                        op.span.set_attribute("app.account.activation.outcome", "pending_elsewhere")
                        return "pending_elsewhere", row["id"]
                    op.conn.execute("DELETE FROM customers WHERE id = %s AND status = 'pending_activation'", (row["id"],))
                    outcome = "recreated_abandoned"
                cid = self._insert_customer(op.conn, email, name, "pending_activation", "assistant", None, address)
                if cid is None:
                    continue  # lost an insert race; re-read
                self._new_claim(op.conn, cid, session_hash, code_hash)
                op.span.set_attribute("app.customer.id", cid)
                op.span.set_attribute("app.account.activation.outcome", outcome)
                return outcome, cid
            op.outcome = "conflict"
            return "exists", None

    def claim_is_live(self, customer_id, session_hash):
        """Validate a Redis handoff against the authoritative Postgres claim."""
        with self._op("claim.validate") as op:
            row = op.conn.execute(
                "SELECT c.email FROM activation_claims a JOIN customers c ON c.id = a.customer_id "
                "WHERE a.customer_id = %s AND a.session_id_hash = %s AND a.used_at IS NULL "
                "AND a.expires_at > now() AND c.status = 'pending_activation'",
                (customer_id, session_hash)).fetchone()
            if row is None:
                op.outcome = "not_found"
                return None
            return row["email"]

    def regenerate_claim(self, session_hash, code_hash):
        """Rotate the code of THIS session's own live claim (protocol 4.2). -> (customer_id, email) | None."""
        with self._op("claim.regenerate") as op:
            row = op.conn.execute(
                "UPDATE activation_claims a SET code_hash = %s, expires_at = now() + make_interval(secs => %s), "
                "rotations = rotations + 1 FROM customers c WHERE c.id = a.customer_id AND c.status = 'pending_activation' "
                "AND a.session_id_hash = %s AND a.used_at IS NULL AND a.expires_at > now() "
                "RETURNING c.id::text AS id, c.email, c.name",
                (code_hash, CLAIM_TTL_SECONDS, session_hash)).fetchone()
            if row is None:
                op.outcome = "not_found"
                return None
            op.span.set_attribute("app.customer.id", row["id"])
            op.span.set_attribute("app.customer.email", row["email"])
            op.span.set_attribute("app.account.activation.outcome", "regenerated")
            return row

    def consume_claim(self, customer_id, session_hash, code_hash, password_hash):
        """Atomic activation (protocol 4.3). -> customer dict | None."""
        with self._op("claim.consume") as op:
            ok = op.conn.execute(
                "UPDATE activation_claims SET used_at = now() WHERE customer_id = %s AND session_id_hash = %s "
                "AND code_hash = %s AND used_at IS NULL AND expires_at > now() RETURNING customer_id::text AS id",
                (customer_id, session_hash, code_hash)).fetchone()
            if ok is None:
                op.outcome = "conflict"
                op.span.set_attribute("app.account.activation.outcome", "failed")
                return None
            row = op.conn.execute(
                "UPDATE customers SET password_hash = %s, status = 'active', updated_at = now() "
                "WHERE id = %s AND status = 'pending_activation' RETURNING id::text AS id, email, name",
                (password_hash, customer_id)).fetchone()
            if row is None:
                op.conn.rollback()
                op.outcome = "conflict"
                return None
            op.conn.execute("INSERT INTO login_events (customer_id, outcome) VALUES (%s, 'activated')", (customer_id,))
            op.span.set_attribute("app.customer.id", row["id"])
            op.span.set_attribute("app.customer.email", row["email"])
            op.span.set_attribute("app.account.activation.outcome", "consumed")
            return row

    def register_web(self, email, name, address, password_hash, currency="USD"):
        """-> (outcome, customer). outcome: created | exists."""
        with self._op("customer.create", **{"app.customer.email": email, "app.customer.name": name}) as op:
            for _ in range(3):
                cid = self._insert_customer(op.conn, email, name, "active", "web", password_hash, address, currency)
                if cid:
                    op.span.set_attribute("app.customer.id", cid)
                    return "created", {"id": cid, "email": email, "name": name}
                row = op.conn.execute(self._LOCK_ROW, (email,)).fetchone()
                if row is None:
                    continue
                if row["status"] == "pending_activation" and not (row["live"] and not row["used"]):
                    op.conn.execute("DELETE FROM customers WHERE id = %s AND status = 'pending_activation'", (row["id"],))
                    continue
                op.outcome = "conflict"
                return "exists", None
            op.outcome = "conflict"
            return "exists", None

    def update_customer(self, email, name=None, preferred_currency=None, default_payment_method_id=None, address=None):
        with self._op("customer.update", **{"app.customer.email": email}) as op:
            row = op.conn.execute(
                "UPDATE customers SET name = coalesce(%s, name), preferred_currency = coalesce(%s, preferred_currency), "
                "default_payment_method_id = coalesce(%s, default_payment_method_id), updated_at = now() "
                "WHERE email = %s RETURNING id::text AS id",
                (name, preferred_currency, default_payment_method_id, email)).fetchone()
            if row is None:
                op.outcome = "not_found"
                return False
            if address:
                self._upsert_address(op.conn, row["id"], address)
            op.span.set_attribute("app.customer.id", row["id"])
            return True

    def record_login_event(self, customer_id, outcome):
        with self._op("login_event") as op:
            op.conn.execute("INSERT INTO login_events (customer_id, outcome) VALUES (%s, %s)", (customer_id, outcome))

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
