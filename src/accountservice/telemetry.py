"""OpenTelemetry setup and helpers for accountservice.

* Traces/metrics (and optional logs) are exported over OTLP; spans pass through the sanitizing
  exporter (telemetry_sanitize) as the last gate. Nothing here records request/response bodies,
  SQL bind parameters or Redis arguments.
* Business attributes go through set_attrs(), which coerces every value through telemetry_schema
  (unknown keys and invalid values are dropped and counted).
* Baggage is filtered to an allowlist on extract and inject, so PII canaries arriving in a
  `baggage` header are dropped at the edge of this process.
"""
import hashlib
import hmac
import json
import logging
import os
import sys
import time

from opentelemetry import baggage, context, metrics, trace
from opentelemetry.baggage.propagation import W3CBaggagePropagator
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.resources import Resource, get_aggregated_resources, OTELResourceDetector
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

import config
import telemetry_sanitize as san
import telemetry_schema as schema

BAGGAGE_ALLOWLIST = frozenset({"app.customer.id", "app.customer.authenticated", "app.order.id",
                               "app.session.ref", "app.order.channel", "app.prompt.version"})

tracer = trace.get_tracer("accountservice")
_meter = metrics.get_meter("accountservice")

account_actions = _meter.create_counter("app.account.actions", description="Account actions by action and outcome")
account_validation_failures = _meter.create_counter("app.account.validation_failures", description="Validation failures by field and code")
account_login = _meter.create_counter("app.account.login", description="Login outcomes")
account_db_duration = _meter.create_histogram("app.account.db.duration", unit="s", description="Account DB operation latency")
account_activation_events = _meter.create_counter("app.account.activation.events", description="Activation protocol events")
account_pending_reaped = _meter.create_counter("app.account.pending_reaped", description="Abandoned pending accounts reaped")
payment_tokenize = _meter.create_counter("app.payment.tokenize", description="Demo-card tokenization outcomes")
orders_recorded = _meter.create_counter("app.orders.recorded", description="Orders written to Postgres by path")
orders_outbox_dead = _meter.create_counter("app.orders.outbox.dead_total", description="Order messages moved to the dead-letter stream")
orders_outbox_retries = _meter.create_counter("app.orders.outbox.retries", description="Order message delivery retries")
orders_persist_latency = _meter.create_histogram("app.orders.persistence.latency", unit="ms", description="Publish-to-commit latency")
payment_tokenize = _meter.create_counter("app.payment.tokenize", description="Payment tokenization outcomes")
invalid_attr = _meter.create_counter("telemetry.invalid_attr", description="Attributes dropped by the typed schema")
rpc_duration = _meter.create_histogram("app.account.rpc.duration", unit="s", description="AccountService RPC latency")


# --- setup -------------------------------------------------------------------------------------
def _endpoint():
    ep = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or os.environ.get("COLLECTOR_SERVICE_ADDR", "localhost:4317")
    return ep if ep.startswith("http") else f"http://{ep}"


def _resource():
    attrs = {"service.name": config.SERVICE_NAME}
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
    return get_aggregated_resources([OTELResourceDetector()]).merge(Resource.create(attrs))


class AllowlistBaggagePropagator(W3CBaggagePropagator):
    """W3C baggage restricted to BAGGAGE_ALLOWLIST on both extract and inject."""

    def extract(self, carrier, context=None, getter=None):
        from opentelemetry.propagators.textmap import default_getter
        ctx = super().extract(carrier, context=context, getter=getter or default_getter)
        kept = {k: v for k, v in baggage.get_all(ctx).items() if k in BAGGAGE_ALLOWLIST}
        ctx = baggage.clear(ctx)
        for k, v in kept.items():
            ctx = baggage.set_baggage(k, v, ctx)
        return ctx

    def inject(self, carrier, context=None, setter=None):
        from opentelemetry import context as ctxapi
        from opentelemetry.propagators.textmap import default_setter
        ctx = context if context is not None else ctxapi.get_current()
        kept = {k: v for k, v in baggage.get_all(ctx).items() if k in BAGGAGE_ALLOWLIST}
        clean = baggage.clear(ctx)
        for k, v in kept.items():
            clean = baggage.set_baggage(k, v, clean)
        super().inject(carrier, context=clean, setter=setter or default_setter)


def init(grpc_server=True):
    """Configure providers + instrumentation. Returns True if exporting."""
    enabled = os.environ.get("ENABLE_TRACING") == "1"
    if enabled:
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.metrics import MeterProvider
        from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        ep, res = _endpoint(), _resource()
        tp = TracerProvider(resource=res)
        tp.add_span_processor(BatchSpanProcessor(san.make_span_exporter(OTLPSpanExporter(endpoint=ep, insecure=True))))
        trace.set_tracer_provider(tp)
        interval = int(os.environ.get("OTEL_METRIC_EXPORT_INTERVAL", "15000"))
        reader = PeriodicExportingMetricReader(OTLPMetricExporter(endpoint=ep, insecure=True), export_interval_millis=interval)
        metrics.set_meter_provider(MeterProvider(resource=res, metric_readers=[reader]))
    set_global_textmap(CompositePropagator([TraceContextTextMapPropagator(), AllowlistBaggagePropagator()]))

    from opentelemetry.instrumentation.grpc import GrpcInstrumentorClient, GrpcInstrumentorServer
    from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor

    GrpcInstrumentorClient().instrument()
    if grpc_server:
        GrpcInstrumentorServer().instrument()
    PsycopgInstrumentor().instrument()      # no sqlcommenter, no parameter capture
    RedisInstrumentor().instrument()        # no argument capture beyond the command name
    _configure_logging()
    return enabled


# --- typed business attributes ---------------------------------------------------------------
def set_attrs(span, attrs=None, **kw):
    """Set app.* attributes through the typed schema. Invalid or unknown ones are dropped."""
    items = dict(attrs or {})
    items.update({k.replace("__", "."): v for k, v in kw.items()})
    for key, value in items.items():
        if value is None:
            continue
        try:
            span.set_attribute(key, schema.coerce(key, value))
        except ValueError:
            invalid_attr.add(1, {"key": key if key in schema.SCHEMA else "unknown"})


def stamp_app_attrs(span=None):
    """Copy allowlisted baggage entries onto the span (typed)."""
    span = span or trace.get_current_span()
    for key, value in baggage.get_all().items():
        if key in BAGGAGE_ALLOWLIST:
            if key == "app.customer.authenticated":
                value = str(value).lower() == "true"
            set_attrs(span, {key: value})


def attach_baggage(**entries):
    ctx = context.get_current()
    for k, v in entries.items():
        k = k.replace("__", ".")
        if k in BAGGAGE_ALLOWLIST and v is not None and v != "":
            ctx = baggage.set_baggage(k, str(v).lower() if isinstance(v, bool) else str(v), ctx)
    return context.attach(ctx)


def detach_baggage(token):
    context.detach(token)


def session_ref(session_id):
    """Opaque, stable reference for a browser session. Never the raw id; omitted without a key."""
    if not session_id or not config.TELEMETRY_REF_KEY:
        return None
    return hmac.new(config.TELEMETRY_REF_KEY.encode(), str(session_id).encode(), hashlib.sha256).hexdigest()[:16]


def trace_id_hex():
    ctx = trace.get_current_span().get_span_context()
    return format(ctx.trace_id, "032x") if ctx.is_valid else ""


def record_exception(span, exc):
    """Type only - exception messages can echo user input."""
    span.set_attribute("error.type", type(exc).__name__)
    span.set_status(trace.Status(trace.StatusCode.ERROR, type(exc).__name__))


# --- structured logs ---------------------------------------------------------------------------
class _JsonFormatter(logging.Formatter):
    def format(self, record):
        out = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + f".{int(record.msecs):03d}Z",
               "severity": record.levelname.lower(), "message": record.getMessage()}
        ctx = trace.get_current_span().get_span_context()
        if ctx.is_valid:
            out["trace_id"] = format(ctx.trace_id, "032x")
            out["span_id"] = format(ctx.span_id, "016x")
        out.update(getattr(record, "fields", {}))
        return json.dumps(out, default=str)


def _configure_logging():
    log = logging.getLogger("accountservice")
    log.handlers.clear()
    log.setLevel(logging.INFO)
    log.propagate = False
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(_JsonFormatter())
    log.addHandler(h)


def _safe_fields(fields):
    clean = {}
    for k, v in fields.items():
        k = k.replace("__", ".")
        if k.startswith("app."):
            try:
                clean[k] = schema.coerce(k, v)
            except ValueError:
                continue
        elif isinstance(v, (bool, int, float)) or (isinstance(v, str) and len(v) <= 48 and v.replace("_", "").replace(".", "").replace("-", "").isalnum()):
            clean[k] = v
    return clean


def event(message, **fields):
    logging.getLogger("accountservice").info(message, extra={"fields": _safe_fields(fields)})


def warn(message, **fields):
    logging.getLogger("accountservice").warning(message, extra={"fields": _safe_fields(fields)})
