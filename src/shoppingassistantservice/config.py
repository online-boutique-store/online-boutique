"""Environment-driven configuration. Nothing here is logged or exported."""
import os


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


# --- Snowflake Cortex (OpenAI-compatible Chat Completions) -----------------
# e.g. https://<account>.snowflakecomputing.com/api/v2/cortex/v1
CORTEX_BASE_URL = os.environ.get("CORTEX_BASE_URL", "").rstrip("/")
CORTEX_PAT = os.environ.get("CORTEX_PAT", "")
CORTEX_MODEL = os.environ.get("CORTEX_MODEL", "claude-sonnet-4-5")
CORTEX_TIMEOUT_S = _int("CORTEX_TIMEOUT_S", 60)

# --- Cost controls ----------------------------------------------------------
MAX_TOOL_STEPS = _int("MAX_TOOL_STEPS", 5)
MAX_COMPLETION_TOKENS = _int("MAX_COMPLETION_TOKENS", 1024)
RATE_LIMIT_RPM = _int("RATE_LIMIT_RPM", 30)  # LLM calls per minute, 0 = off
LLM_RETRIES = _int("LLM_RETRIES", 2)
HISTORY_TURNS = _int("HISTORY_TURNS", 4)
MAX_SESSIONS = _int("MAX_SESSIONS", 500)

# --- Incident configuration -------------------------------------------------
# PROMPT_VERSION selects the system prompt + tool descriptions (see incident.py).
PROMPT_VERSION = os.environ.get("PROMPT_VERSION", "v1").strip().lower()
# FAULT_INJECTION is a comma-separated list of SYNTHETIC faults applied by this
# service (not by the model). Empty by default. See incident.FAULTS.
FAULT_INJECTION = {
    f.strip() for f in os.environ.get("FAULT_INJECTION", "").split(",") if f.strip()
}

# --- Backends (same env names the other services use) -----------------------
CATALOG_ADDR = os.environ.get("PRODUCT_CATALOG_SERVICE_ADDR", "productcatalogservice:3550")
CART_ADDR = os.environ.get("CART_SERVICE_ADDR", "cartservice:7070")
CURRENCY_ADDR = os.environ.get("CURRENCY_SERVICE_ADDR", "currencyservice:7000")
CHECKOUT_ADDR = os.environ.get("CHECKOUT_SERVICE_ADDR", "checkoutservice:5050")
GRPC_TIMEOUT_S = _int("GRPC_TIMEOUT_S", 15)

PORT = _int("PORT", 8080)


def cortex_configured():
    return bool(CORTEX_BASE_URL and CORTEX_PAT)
