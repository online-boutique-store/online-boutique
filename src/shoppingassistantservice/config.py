"""Environment-driven configuration. Nothing here is logged or exported."""
import os


def _int(name, default):
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


# --- Amazon Bedrock (Converse API) ------------------------------------------
# Credentials come from the standard AWS chain (EKS Pod Identity / IRSA in the
# cluster, your local profile when testing) - never from this file or env vars
# set here. BEDROCK_MODEL_ID must be a model or inference-profile ID your
# account has access to; the default is a US cross-region inference profile and
# is NOT verified against any account. Confirm with:
#   aws bedrock list-inference-profiles
AWS_REGION = os.environ.get("AWS_REGION") or os.environ.get("AWS_DEFAULT_REGION", "us-west-2")
BEDROCK_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
# Only for local fakes/tests; leave unset in real deployments.
BEDROCK_ENDPOINT_URL = os.environ.get("BEDROCK_ENDPOINT_URL") or None
BEDROCK_TIMEOUT_S = _int("BEDROCK_TIMEOUT_S", 60)

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


def bedrock_configured():
    return bool(BEDROCK_MODEL_ID and AWS_REGION)
