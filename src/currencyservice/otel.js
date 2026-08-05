'use strict';

function initTelemetry(serviceName) {
  if (process.env.ENABLE_TRACING !== '1') {
    return;
  }

  const { NodeSDK } = require('@opentelemetry/sdk-node');
  const { Resource } = require('@opentelemetry/resources');
  const { registerInstrumentations } = require('@opentelemetry/instrumentation');
  const { GrpcInstrumentation } = require('@opentelemetry/instrumentation-grpc');

  // Register before OTLP gRPC exporters load @grpc/grpc-js (required for server spans).
  registerInstrumentations({
    instrumentations: [new GrpcInstrumentation()],
  });

  const { OTLPTraceExporter } = require('@opentelemetry/exporter-trace-otlp-grpc');
  const { OTLPMetricExporter } = require('@opentelemetry/exporter-metrics-otlp-grpc');
  const { OTLPLogExporter } = require('@opentelemetry/exporter-logs-otlp-grpc');
  const { PeriodicExportingMetricReader } = require('@opentelemetry/sdk-metrics');
  const { BatchLogRecordProcessor } = require('@opentelemetry/sdk-logs');
  // Baggage propagation is enabled via the OTEL_PROPAGATORS=tracecontext,baggage
  // env var (set in the observe-otel-env ConfigMap), which the NodeSDK reads
  // automatically — no manual propagator setup needed here.

  const collectorUrl = process.env.OTEL_EXPORTER_OTLP_ENDPOINT
    || process.env.COLLECTOR_SERVICE_ADDR
    || 'http://localhost:4317';
  const url = collectorUrl.startsWith('http') ? collectorUrl : `http://${collectorUrl}`;

  let deploymentEnv = 'production';
  for (const pair of (process.env.OTEL_RESOURCE_ATTRIBUTES || '').split(',')) {
    const eq = pair.indexOf('=');
    if (eq === -1) continue;
    if (pair.slice(0, eq).trim() === 'deployment.environment.name') {
      deploymentEnv = pair.slice(eq + 1).trim();
      break;
    }
  }

  const resource = new Resource({
    'service.name': process.env.OTEL_SERVICE_NAME || serviceName,
    'deployment.environment.name': deploymentEnv,
    'deployment.environment': deploymentEnv,
    ...(process.env.K8S_POD_NAME ? { 'k8s.pod.name': process.env.K8S_POD_NAME } : {}),
    ...(process.env.K8S_NAMESPACE ? { 'k8s.namespace.name': process.env.K8S_NAMESPACE } : {}),
  });

  const sdkOptions = {
    resource,
    traceExporter: new OTLPTraceExporter({ url }),
    metricReader: new PeriodicExportingMetricReader({
      exporter: new OTLPMetricExporter({ url }),
      exportIntervalMillis: 15000,
    }),
  };

  if (process.env.OTEL_LOGS_EXPORTER === 'otlp') {
    sdkOptions.logRecordProcessor = new BatchLogRecordProcessor(
      new OTLPLogExporter({ url }),
    );
  }

  const sdk = new NodeSDK(sdkOptions);

  sdk.start();
  process.on('SIGTERM', () => sdk.shutdown());
}

/**
 * Copy every app.* baggage entry onto the active span. Call at the entry of
 * every gRPC handler so each span carries all attributes that exist at that
 * point in the request flow.
 */
function stampAppAttrs() {
  const api = require('@opentelemetry/api');
  const span = api.trace.getActiveSpan();
  const bag = api.propagation.getActiveBaggage();
  if (!span || !bag) {
    return;
  }
  for (const [key, entry] of Object.entries(bag.getAllEntries())) {
    if (key.startsWith('app.')) {
      span.setAttribute(key, entry.value);
    }
  }
}

module.exports = { initTelemetry, stampAppAttrs };
