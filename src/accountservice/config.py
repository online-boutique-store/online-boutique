"""Environment-driven configuration for accountservice. Nothing here is logged or exported."""
import os


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


PORT = _int("PORT", 8081)
SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "accountservice")

# --- Accounts database (Postgres) ---------------------------------------------------------------
ACCOUNTS_DATABASE_URL = os.environ.get("ACCOUNTS_DATABASE_URL", "")
ACCOUNTS_DB_POOL_MAX = _int("ACCOUNTS_DB_POOL_MAX", 8)
ACCOUNTS_DB_STATEMENT_TIMEOUT_MS = _int("ACCOUNTS_DB_STATEMENT_TIMEOUT_MS", 3000)
ACCOUNTS_MIGRATION_RETRY_MAX_S = _int("ACCOUNTS_MIGRATION_RETRY_MAX_S", 30)

# --- Sessions, lockouts and the activation handoff live in the existing redis-cart ----------------
REDIS_ADDR = os.environ.get("REDIS_ADDR", "redis-cart:6379")
# --- Durable order stream / idempotency records (dedicated, AOF always) ---------------------------
ORDER_QUEUE_ADDR = os.environ.get("ORDER_QUEUE_ADDR", "order-queue:6379")

# --- Internal service credentials: "frontend:secret,checkoutservice:secret,..." --------------------
def service_secrets():
    out = {}
    for pair in os.environ.get("ACCOUNT_SERVICE_SECRETS", "").split(","):
        name, _, secret = pair.partition(":")
        if name.strip() and secret.strip():
            out[name.strip()] = secret.strip()
    return out


# --- Telemetry --------------------------------------------------------------------------------
# Per-deployment key for opaque references (app.session.ref). Missing key => attribute omitted.
TELEMETRY_REF_KEY = os.environ.get("TELEMETRY_REF_KEY", "")

# --- Behaviour ---------------------------------------------------------------------------------
CLAIM_TTL_SECONDS = _int("CLAIM_TTL_SECONDS", 900)
SESSION_TTL_SECONDS = _int("SESSION_TTL_SECONDS", 172800)
LOGIN_MAX_FAILURES = _int("LOGIN_MAX_FAILURES", 5)
LOGIN_LOCKOUT_SECONDS = _int("LOGIN_LOCKOUT_SECONDS", 900)
MAX_ADDRESSES = _int("MAX_ADDRESSES", 10)
MAX_PAYMENT_METHODS = _int("MAX_PAYMENT_METHODS", 5)
PAYMENT_SERVICE_ADDR = os.environ.get("PAYMENT_SERVICE_ADDR", "paymentservice:50051")
# Only these callers may resolve a saved method into a charge token.
TOKEN_RESOLVERS = frozenset(os.environ.get("TOKEN_RESOLVERS", "checkoutservice").split(","))
MAX_PAYMENT_METHODS = _int("MAX_PAYMENT_METHODS", 5)
