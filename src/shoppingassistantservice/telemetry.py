"""OpenTelemetry setup: traces, metrics, logs, plus the demo's business instruments.

Mirrors the other services' conventions (OTLP gRPC to the Observe agent
forwarder, W3C tracecontext+baggage, app.* attributes stamped from baggage).
"""
import json
import logging
import os
import sys
import time

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
        tp.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=ep, insecure=True)))
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
            lp.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(endpoint=ep, insecure=True)))
            set_logger_provider(lp)

    # Carry app.* baggage (set by the frontend) into this service's spans.
    set_global_textmap(CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()]))

    # Even when exporting is off these are harmless no-ops against the API tracer.
    from opentelemetry.instrumentation.botocore import BotocoreInstrumentor
    from opentelemetry.instrumentation.flask import FlaskInstrumentor
    from opentelemetry.instrumentation.grpc import GrpcInstrumentorClient

    FlaskInstrumentor().instrument_app(flask_app, excluded_urls="healthz")
    GrpcInstrumentorClient().instrument()
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


def _json_attr(value):
    s = json.dumps(value, default=str)
    return s if len(s) <= _MAX_MSG_ATTR else s[:_MAX_MSG_ATTR] + "...[truncated]"


def _is_converse(service_name, operation_name):
    return service_name == "bedrock-runtime" and operation_name == "Converse"


def _bedrock_request_hook(span, service_name, operation_name, api_params):
    if not (_is_converse(service_name, operation_name) and span.is_recording()):
        return
    span.set_attribute("gen_ai.provider.name", "aws.bedrock")
    # Observe classifies LLM spans by these Traceloop/OpenLLMetry attributes.
    span.set_attribute("traceloop.span.kind", "task")
    span.set_attribute("llm.request.type", "chat")
    model_id = api_params.get("modelId", "")
    span.set_attribute("llm.request.model", model_id)
    if not _content_enabled():
        return
    msgs = [{"role": "system", "parts": _parts(api_params.get("system"))}] if api_params.get("system") else []
    msgs += [{"role": m.get("role"), "parts": _parts(m.get("content"))} for m in api_params.get("messages", [])]
    span.set_attribute("gen_ai.input.messages", _json_attr(msgs))


def _bedrock_response_hook(span, service_name, operation_name, result):
    if not (_is_converse(service_name, operation_name) and span.is_recording()):
        return
    if not _content_enabled():
        return
    msg = (result or {}).get("output", {}).get("message") or {}
    span.set_attribute("llm.response.model", (result or {}).get("modelId", ""))
    span.set_attribute("gen_ai.output.messages", _json_attr(
        [{"role": msg.get("role", "assistant"), "parts": _parts(msg.get("content")),
          "finish_reason": (result or {}).get("stopReason")}]))


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
    """AppLogs resolves the customer from attributes["UserId"] (cartservice style)
    or the JSON body's "session" key (frontend style). Provide both so assistant
    logs join to Customer_Records without changing that pipeline."""
    sid = fields.get("app.session.id")
    if sid:
        fields.setdefault("UserId", sid)
        fields.setdefault("session", sid)
    return fields


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
