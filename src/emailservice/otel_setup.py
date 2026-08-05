import os

import opentelemetry.baggage as baggage_api
import opentelemetry.metrics as metrics_api
import opentelemetry.trace as trace_api
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.grpc import GrpcInstrumentorClient, GrpcInstrumentorServer
from opentelemetry.propagate import set_global_textmap
from opentelemetry.propagators.composite import CompositePropagator
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import OTELResourceDetector, Resource, get_aggregated_resources
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

try:
    from opentelemetry.baggage.propagation import W3CBaggagePropagator
except ImportError:  # pragma: no cover - older versions
    from opentelemetry.propagators.baggage import W3CBaggagePropagator


def stamp_app_attrs():
    """Copy every app.* baggage entry onto the current span.

    Call at the entry of every gRPC handler so each span carries all
    attributes that exist at that point in the request flow.
    """
    span = trace_api.get_current_span()
    for key, value in baggage_api.get_all().items():
        if key.startswith("app."):
            span.set_attribute(key, value)


def _endpoint():
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or os.environ.get(
        "COLLECTOR_SERVICE_ADDR", "localhost:4317"
    )
    if not endpoint.startswith("http"):
        endpoint = f"http://{endpoint}"
    return endpoint


def _build_resource():
    resource = get_aggregated_resources([OTELResourceDetector()])
    attrs = {}
    if os.environ.get("OTEL_SERVICE_NAME"):
        attrs["service.name"] = os.environ["OTEL_SERVICE_NAME"]
    if os.environ.get("K8S_POD_NAME"):
        attrs["k8s.pod.name"] = os.environ["K8S_POD_NAME"]
    if os.environ.get("K8S_NAMESPACE"):
        attrs["k8s.namespace.name"] = os.environ["K8S_NAMESPACE"]
    for pair in (os.environ.get("OTEL_RESOURCE_ATTRIBUTES") or "").split(","):
        if "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        key, value = key.strip(), value.strip()
        if key and value:
            attrs[key] = value
    if "deployment.environment" not in attrs and attrs.get("deployment.environment.name"):
        attrs["deployment.environment"] = attrs["deployment.environment.name"]
    return resource.merge(Resource.create(attrs)) if attrs else resource


def init_opentelemetry(*, instrument_client=False):
    if os.environ.get("ENABLE_TRACING") != "1":
        return False

    endpoint = _endpoint()
    resource = _build_resource()

    trace_provider = TracerProvider(resource=resource)
    trace_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
    )
    trace_api.set_tracer_provider(trace_provider)
    # Enable W3C Baggage propagation so app.* attributes injected upstream
    # (e.g. app.session.id from the frontend) reach these spans.
    set_global_textmap(
        CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()])
    )
    GrpcInstrumentorServer().instrument()
    if instrument_client:
        GrpcInstrumentorClient().instrument()

    if os.environ.get("OTEL_METRICS_EXPORTER") == "otlp":
        reader = PeriodicExportingMetricReader(
            OTLPMetricExporter(endpoint=endpoint, insecure=True),
            export_interval_millis=15000,
        )
        metrics_api.set_meter_provider(
            MeterProvider(resource=resource, metric_readers=[reader])
        )

    if os.environ.get("OTEL_LOGS_EXPORTER") == "otlp":
        from opentelemetry._logs import set_logger_provider
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
        from opentelemetry.sdk._logs import LoggerProvider
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

        log_provider = LoggerProvider(resource=resource)
        log_provider.add_log_record_processor(
            BatchLogRecordProcessor(OTLPLogExporter(endpoint=endpoint, insecure=True))
        )
        set_logger_provider(log_provider)

    return True


def configure_service_logging(logger):
    """Attach OTLP LoggingHandler so ILogger records export with trace context."""
    if os.environ.get("OTEL_LOGS_EXPORTER") != "otlp":
        return

    import logging

    from opentelemetry._logs import get_logger_provider
    from opentelemetry.sdk._logs import LoggingHandler

    provider = get_logger_provider()
    if provider is None:
        return

    logger.handlers.clear()
    logger.addHandler(LoggingHandler(level=logging.NOTSET, logger_provider=provider))
    logger.setLevel(logging.INFO)
    logger.propagate = False
