"""OpenTelemetry setup: traces, metrics, logs, plus the demo's business instruments.

Mirrors the other services' conventions (OTLP gRPC to the Observe agent
forwarder, W3C tracecontext+baggage, app.* attributes stamped from baggage).
"""
import json
import logging
import os
import re
import sys
import time

import telemetry_sanitize as _san
from opentelemetry import baggage, context, metrics, trace
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.resources import OTELResourceDetector, Resource, get_aggregated_resources
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

try:
    from opentelemetry.baggage.propagation import W3CBaggagePropagator
except ImportError:  # pragma: no cover
    from opentelemetry.propagators.baggage import W3CBaggagePropagator

SERVICE_NAME = os.environ.get("OTEL_SERVICE_NAME", "shoppingassistantservice")
tracer = trace.get_tracer("shoppingassistantservice")
_meter = metrics.get_meter("shoppingassistantservice")

# --- business instruments (low-cardinality dimensions only) -----------------
requests_total = _meter.create_counter(
    "ai.assistant.requests", description="Assistant requests by outcome")
tokens_total = _meter.create_counter(
    "ai.assistant.tokens", unit="{token}", description="LLM tokens consumed by the assistant")
llm_calls_total = _meter.create_counter(
    "ai.assistant.llm_calls", description="LLM calls by outcome")
llm_retries_total = _meter.create_counter(
    "ai.assistant.llm_retries", description="LLM call retries")
tool_calls_total = _meter.create_counter(
    "ai.tool.calls", description="Agent tool executions")
tool_errors_total = _meter.create_counter(
    "ai.tool.errors", description="Agent tool executions that returned an error")
checkout_attempts = _meter.create_counter(
    "ai.checkout.attempts", description="AI-assisted checkout attempts by outcome")
checkout_value = _meter.create_counter(
    "ai.checkout.value_usd", unit="USD",
    description="USD value of AI-assisted checkout attempts, by outcome. "
                "outcome=failed is attempted value, NOT confirmed lost revenue.")
checkout_retries = _meter.create_counter(
    "ai.checkout.retries", description="Checkout attempts that follow an earlier failed attempt in the same session")
checkout_recovered_value = _meter.create_counter(
    "ai.checkout.recovered_value_usd", unit="USD",
    description="USD value of orders that succeeded after an earlier failure in the same session. "
                "lost_revenue = failed value - recovered value.")
checkout_funnel_stage = _meter.create_counter(
    "ai.checkout.funnel.stage",
    description="Unique checkout flows reaching a funnel stage. Counted ONCE per flow per stage "
                "(repeats and retries do not inflate it). prefilled=true when a returning "
                "customer's saved data satisfied the stage.")
checkout_terminal = _meter.create_counter(
    "ai.checkout.terminal",
    description="Terminal outcome per checkout attempt: completed | failed | abandoned. "
                "Exactly one per attempt.")
account_lookups = _meter.create_counter(
    "ai.account.lookups", description="Account lookups by outcome (found | not_found | error)")
account_creations = _meter.create_counter(
    "ai.account.creations", description="Account creations by outcome (created | exists | error)")
account_web_operations = _meter.create_counter(
    "app.account.operations",
    description="Website account operations (register | login | logout | profile_update) by outcome "
                "(success | validation_failed | exists | invalid_credentials | locked | unauthorized)")
currency_selections = _meter.create_counter(
    "ai.checkout.currency_selections",
    description="Currency values chosen by the model for checkout; currency.valid=false means the "
                "value is not an ISO code the currency service accepts.")
account_db_duration = _meter.create_histogram(
    "app.account.db.duration", unit="s", description="Accounts database operation latency by operation and outcome")
account_pending_reaped = _meter.create_counter(
    "app.account.pending_reaped", description="Abandoned pending accounts deleted by the reaper")
account_activation_events = _meter.create_counter(
    "app.account.activation.events", description="Activation lifecycle events by outcome")
request_duration = _meter.create_histogram(
    "ai.assistant.request.duration", unit="s", description="End-to-end agent time per customer prompt")
llm_call_duration = _meter.create_histogram(
    "ai.llm.call.duration", unit="s", description="Bedrock Converse call latency")
tool_duration = _meter.create_histogram(
    "ai.tool.duration", unit="s", description="Agent tool execution time")


# --- setup -------------------------------------------------------------------
def _endpoint():
    ep = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or os.environ.get(
        "COLLECTOR_SERVICE_ADDR", "localhost:4317")
    return ep if ep.startswith("http") else f"http://{ep}"


def _resource():
    res = get_aggregated_resources([OTELResourceDetector()])
    attrs = {"service.name": SERVICE_NAME}
    if os.environ.get("K8S_POD_NAME"):
        attrs["k8s.pod.name"] = os.environ["K8S_POD_NAME"]
    if os.environ.get("K8S_NAMESPACE"):
        attrs["k8s.namespace.name"] = os.environ["K8S_NAMESPACE"]
    for pair in (os.environ.get("OTEL_RESOURCE_ATTRIBUTES") or "").split(","):
        if "=" in pair:
            k, v = pair.split("=", 1)
            if k.strip() and v.strip():
                attrs[k.strip()] = v.strip()
    if "deployment.environment" not in attrs and attrs.get("deployment.environment.name"):
        attrs["deployment.environment"] = attrs["deployment.environment.name"]
    return res.merge(Resource.create(attrs))


def init(flask_app):
    """Configure providers + instrumentation. Returns True if exporting."""
    enabled = os.environ.get("ENABLE_TRACING") == "1"
    otlp_logs = enabled and os.environ.get("OTEL_LOGS_EXPORTER") == "otlp"

    if enabled:
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        ep, res = _endpoint(), _resource()
        tp = TracerProvider(resource=res)
        tp.add_span_processor(BatchSpanProcessor(
            _san.make_span_exporter(OTLPSpanExporter(endpoint=ep, insecure=True))))
        trace.set_tracer_provider(tp)
        if os.environ.get("OTEL_METRICS_EXPORTER", "otlp") == "otlp":
            reader = PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=ep, insecure=True), export_interval_millis=15000)
            metrics.set_meter_provider(MeterProvider(resource=res, metric_readers=[reader]))
        if otlp_logs:
            from opentelemetry._logs import set_logger_provider
            from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
            from opentelemetry.sdk._logs import LoggerProvider
            from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

            lp = LoggerProvider(resource=res)
            # Sanitizer first: it mutates the record before the batch processor queues it.
            lp.add_log_record_processor(_san.make_log_processor())
            lp.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(endpoint=ep, insecure=True)))
            set_logger_provider(lp)

    # Carry app.* baggage (set by the frontend) into this service's spans.
    set_global_textmap(CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()]))

    # Even when exporting is off these are harmless no-ops against the API tracer.
    from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
    from opentelemetry.instrumentation.flask import FlaskInstrumentor
    from opentelemetry.instrumentation.grpc import GrpcInstrumentorClient
    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor

    FlaskInstrumentor().instrument_app(flask_app, excluded_urls="healthz")
    GrpcInstrumentorClient().instrument()
    # No sqlcommenter and no parameter capture: SQL spans carry placeholders only.
    PsycopgInstrumentor().instrument()
    try:  # Redis client spans for account reads/writes (child of the execute_tool span)
        from opentelemetry.instrumentation.redis import RedisInstrumentor
        RedisInstrumentor().instrument()
    except ImportError:
        pass
    # Botocore instruments Bedrock Converse as a `chat <model>` span with model +
    # token usage. It only emits prompt/response content as gen_ai.* LOG events
    # (when OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT=true), never as span
    # attributes - so the hooks below also put semconv messages on the span itself.
    BotocoreInstrumentor().instrument(request_hook=_bedrock_request_hook,
                                      response_hook=_bedrock_response_hook)
    _configure_logging(otlp_logs)
    return enabled


# --- Bedrock chat span enrichment ---------------------------------------------
_MAX_MSG_ATTR = 8000


def _content_enabled():
    return os.environ.get("OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT", "").strip().lower() == "true"


def _parts(blocks):
    """Converse content blocks -> OTel GenAI semconv message parts."""
    out = []
    for b in blocks or []:
        if "text" in b:
            out.append({"type": "text", "content": b["text"]})
        elif "toolUse" in b:
            t = b["toolUse"]
            out.append({"type": "tool_call", "id": t.get("toolUseId"), "name": t.get("name"),
                        "arguments": t.get("input")})
        elif "toolResult" in b:
            t = b["toolResult"]
            out.append({"type": "tool_call_response", "id": t.get("toolUseId"),
                        "response": [c.get("json", c.get("text")) for c in t.get("content", [])]})
    return out


_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")


def redact(s):
    """Preserve synthetic identity and regression evidence while excluding shipping/secrets."""
    try:
        return _san.sanitize_text(s)
    except Exception:
        return _san.CONTENT_PLACEHOLDER


def _json_attr(value):
    s = redact(json.dumps(value, default=str))
    return s if len(s) <= _MAX_MSG_ATTR else s[:_MAX_MSG_ATTR] + "...[truncated]"


def _message_text(message):
    parts = []
    for part in message.get("parts") or []:
        if part.get("type") == "text":
            parts.append(str(part.get("content") or ""))
        else:
            parts.append(json.dumps(part, default=str))
    return "\n".join(part for part in parts if part)


def _set_legacy_message_attributes(span, prefix, messages):
    for index, message in enumerate(messages):
        content = _message_text(message)
        if not content:
            continue
        span.set_attribute(f"{prefix}.{index}.role", str(message.get("role") or ""))
        span.set_attribute(f"{prefix}.{index}.content", redact(content)[:_MAX_MSG_ATTR])


def set_message_attributes(span, input_messages=None, output_messages=None, system_instructions=None):
    """Write both modern message arrays and Observe's indexed text attributes.

    system_instructions (a list of semconv parts) goes in gen_ai.system_instructions,
    never in input_messages, so index 0 of the inputs is a conversation turn."""
    if not (_content_enabled() and span.is_recording()):
        return
    if system_instructions:
        span.set_attribute("gen_ai.system_instructions", _json_attr(system_instructions))
    if input_messages is not None:
        span.set_attribute("gen_ai.input.messages", _json_attr(input_messages))
        _set_legacy_message_attributes(span, "gen_ai.prompt", input_messages)
    if output_messages is not None:
        span.set_attribute("gen_ai.output.messages", _json_attr(output_messages))
        _set_legacy_message_attributes(span, "gen_ai.completion", output_messages)


def _is_converse(service_name, operation_name):
    return service_name == "bedrock-runtime" and operation_name == "Converse"


def _bedrock_request_hook(span, service_name, operation_name, api_params):
    if not (_is_converse(service_name, operation_name) and span.is_recording()):
        return
    span.set_attribute("gen_ai.provider.name", "aws.bedrock")
    # Observe classifies LLM spans by these Traceloop/OpenLLMetry attributes.
    span.set_attribute("traceloop.span.kind", "chat")
    span.set_attribute("llm.request.type", "chat")
    span.set_attribute("gen_ai.operation.name", "chat")
    stamp_app_attrs(span)
    model_id = api_params.get("modelId", "")
    span.set_attribute("gen_ai.request.model", model_id)
    span.set_attribute("llm.request.model", model_id)
    tools = (api_params.get("toolConfig") or {}).get("tools") or []
    defs = [{"name": t["toolSpec"].get("name"), "description": t["toolSpec"].get("description"),
             "parameters": (t["toolSpec"].get("inputSchema") or {}).get("json")}
            for t in tools if "toolSpec" in t]
    if defs:
        span.set_attribute("gen_ai.tool.definitions", json.dumps(defs, default=str))
    if not _content_enabled():
        return
    msgs = [{"role": m.get("role"), "parts": _parts(m.get("content"))} for m in api_params.get("messages", [])]
    set_message_attributes(span, input_messages=msgs,
                           system_instructions=_parts(api_params.get("system")) or None)


def _bedrock_response_hook(span, service_name, operation_name, result):
    if not (_is_converse(service_name, operation_name) and span.is_recording()):
        return
    # The Converse response carries no modelId, so fall back to the request model.
    req_model = (getattr(span, "attributes", None) or {}).get("gen_ai.request.model", "")
    model = (result or {}).get("modelId") or req_model
    if model:
        span.set_attribute("gen_ai.response.model", model)
        span.set_attribute("llm.response.model", model)
    usage = (result or {}).get("usage") or {}
    if usage:
        in_tok, out_tok = usage.get("inputTokens", 0), usage.get("outputTokens", 0)
        span.set_attribute("gen_ai.usage.input_tokens", in_tok)
        span.set_attribute("gen_ai.usage.output_tokens", out_tok)
        span.set_attribute("llm.usage.total_tokens", usage.get("totalTokens", in_tok + out_tok))
    if not _content_enabled():
        return
    msg = (result or {}).get("output", {}).get("message") or {}
    set_message_attributes(span, output_messages=[
        {"role": msg.get("role", "assistant"), "parts": _parts(msg.get("content")),
         "finish_reason": (result or {}).get("stopReason")}
    ])


# --- structured logging with trace correlation -------------------------------
class _JsonFormatter(logging.Formatter):
    """One JSON object per line, same keys the frontend's logrus emits, plus
    trace_id/span_id so AppLogs can correlate when stdout is the transport."""

    def format(self, record):
        out = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "severity": record.levelname.lower(),
            "message": record.getMessage(),
        }
        ctx = trace.get_current_span().get_span_context()
        if ctx.is_valid:
            out["trace_id"] = format(ctx.trace_id, "032x")
            out["span_id"] = format(ctx.span_id, "016x")
        out.update(getattr(record, "fields", {}))
        return json.dumps(out, default=str)


def _configure_logging(otlp_logs):
    log = logging.getLogger("assistant")
    log.handlers.clear()
    log.setLevel(logging.INFO)
    log.propagate = False
    if otlp_logs:
        # OTLP path: the SDK handler stamps trace_id/span_id on each record.
        from opentelemetry._logs import get_logger_provider
        from opentelemetry.sdk._logs import LoggingHandler

        log.addHandler(LoggingHandler(level=logging.NOTSET, logger_provider=get_logger_provider()))
    else:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(_JsonFormatter())
        log.addHandler(h)


def _identity(fields):
    """Logs carry an opaque per-session reference only. The raw browser session id is never exported
    (it is a bearer-ish identifier); UserId/session aliases keep the per-session correlation key that
    log pipelines expect, but hold the HMAC reference instead."""
    fields.pop("app.session.id", None)          # never export the raw id
    ref = fields.get("app.session.ref")
    if ref:
        fields.setdefault("UserId", ref)
        fields.setdefault("session", ref)
    return fields


_UUID_RE = __import__("re").compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def customer_uuid(value):
    """The Postgres customer UUID if `value` is one, else None (legacy ids / session ids never qualify)."""
    v = str(value or "").lower()
    return v if _UUID_RE.match(v) else None


def identity_attrs(session_id, customer_id=None):
    """The only identity attributes the assistant exports: app.session.ref, and app.customer.id +
    app.customer.authenticated for a signed-in customer (Postgres UUID). No name, email or hash."""
    out = {}
    ref = session_ref(session_id)
    if ref:
        out["app.session.ref"] = ref
    cid = customer_uuid(customer_id)
    out["app.customer.authenticated"] = bool(cid)
    if cid:
        out["app.customer.id"] = cid
    return out


def event(message, **fields):
    """Emit a structured business/outcome log. `fields` become log attributes."""
    fields = _identity(fields)
    logging.getLogger("assistant").info(message, extra={"fields": fields} if _stdout_mode() else fields)


def warn(message, **fields):
    fields = _identity(fields)
    logging.getLogger("assistant").warning(message, extra={"fields": fields} if _stdout_mode() else fields)


def _stdout_mode():
    return not any(h.__class__.__name__ == "LoggingHandler" for h in logging.getLogger("assistant").handlers)


# --- baggage helpers ----------------------------------------------------------
def stamp_app_attrs(span=None):
    """Copy every app.* baggage entry onto the span (same helper the other services have)."""
    span = span or trace.get_current_span()
    for key, value in baggage.get_all().items():
        if key.startswith("app."):
            span.set_attribute(key, value)
            # Observe's AI Agents > Conversation view groups by the standard gen_ai /
            # user attributes, so mirror our app.* identity onto every span in the turn.
            for std_key in _STANDARD_MIRRORS.get(key, ()):
                span.set_attribute(std_key, value)
    # Standard user attributes: the signed-in customer's UUID, else the opaque session reference.
    bag = baggage.get_all()
    who = bag.get("app.customer.id") or bag.get("app.session.ref")
    if who:
        span.set_attribute("user.id", who)
        span.set_attribute("enduser.id", who)


_STANDARD_MIRRORS = {
    "app.conversation.id": ("gen_ai.conversation.id",),
}

_PROCESS_KEY = __import__("os").urandom(16)


def opaque_fallback(session_id):
    """Stable-within-process opaque id used only when TELEMETRY_REF_KEY is not configured."""
    import hashlib
    import hmac
    return hmac.new(_PROCESS_KEY, str(session_id).encode(), hashlib.sha256).hexdigest()[:16]


def session_ref(session_id):
    """Opaque, stable reference for a browser session (HMAC, 16 hex chars). Empty without TELEMETRY_REF_KEY."""
    import hashlib
    import hmac
    key = os.environ.get("TELEMETRY_REF_KEY", "")
    if not session_id or not key:
        return ""
    return hmac.new(key.encode(), str(session_id).encode(), hashlib.sha256).hexdigest()[:16]


def attach_baggage(**entries):
    """Add baggage entries to the current context so outgoing gRPC calls carry them.
    Returns a token for detach_baggage()."""
    ctx = context.get_current()
    for k, v in entries.items():
        if v:
            ctx = baggage.set_baggage(k, str(v), ctx)
    return context.attach(ctx)


def detach_baggage(token):
    context.detach(token)


def trace_id_hex():
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else ""
