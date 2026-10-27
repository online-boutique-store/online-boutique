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
    traceExporter: sanitizingExporter(new OTLPTraceExporter({ url })),
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
  process.on('SIGTERM', () => sdk.shutdown().finally(() => process.exit(0)));
}

// ---- typed attributes + fail-closed export sanitizer -------------------------------------------
const SAFE_ID = /^[A-Za-z0-9._:-]{1,64}$/;
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const APP_SCHEMA = {
  'app.customer.id': (v) => typeof v === 'string' && UUID.test(v),
  'app.customer.authenticated': (v) => typeof v === 'boolean',
  'app.order.id': (v) => typeof v === 'string' && UUID.test(v),
  'app.session.ref': (v) => typeof v === 'string' && /^[0-9a-f]{16}$/.test(v),
  'app.order.channel': (v) => v === 'web' || v === 'assistant',
  'app.prompt.version': (v) => v === 'v1' || v === 'v2',
  'app.payment.method_type': (v) => v === 'demo_card_saved' || v === 'card_form',
  'app.payment.status': (v) => ['tokenized', 'charged', 'declined', 'uncertain', 'failed', 'replayed'].includes(v),
  'app.payment.provider': (v) => v === 'demo-psp',
  'app.payment.transaction_id': (v) => typeof v === 'string' && SAFE_ID.test(v),
  'app.request.id': (v) => typeof v === 'string' && SAFE_ID.test(v),
  'app.currency': (v) => typeof v === 'string' && /^[A-Z]{3}$/.test(v),
};
const BAGGAGE_ALLOW = new Set(['app.customer.id', 'app.customer.authenticated', 'app.order.id', 'app.session.ref', 'app.order.channel',
  'app.prompt.version', 'app.request.id', 'app.currency']);
const DROP_PREFIXES = ['http.request.header', 'http.response.header', 'url.query', 'url.full', 'http.url', 'exception.message',
  'exception.stacktrace', 'db.query.parameter', 'rpc.request.metadata', 'rpc.response.metadata'];
const TEXT_PATTERNS = [
  [/[A-Za-z0-9._%+'-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+/g, '<contact_redacted>'],
  [/\btok_demo_[A-Za-z0-9_-]+/g, '<secret_redacted>'],
  [/\b(?:\d[ -]?){12,18}\d\b/g, '<card>'],
  [/(?:^|[^\w.])(?:\+?1[\s.-]?)?\(?[2-9]\d{2}\)?[\s.-]?[2-9]\d{2}[\s.-]?\d{4}(?:[^\w]|$)/g, '<contact_redacted>'],
];

function scrub(v) {
  if (typeof v !== 'string') return v;
  let out = v;
  for (const [re, rep] of TEXT_PATTERNS) out = out.replace(re, rep);
  return out;
}

function sanitizeAttrs(attrs) {
  const out = {};
  for (const [k, v] of Object.entries(attrs || {})) {
    if (DROP_PREFIXES.some((p) => k.startsWith(p))) continue;
    if (k.startsWith('app.')) { const f = APP_SCHEMA[k]; if (f && f(v)) out[k] = v; continue; }
    if (k === 'http.target' || k === 'url.path') { out[k] = scrub(String(v).split('?')[0]); continue; }
    out[k] = Array.isArray(v) ? v.map(scrub) : scrub(v);
  }
  return out;
}

// Wraps the OTLP span exporter: attributes/events/links are rewritten in place before export.
function sanitizingExporter(inner) {
  if (process.env.TELEMETRY_TEST_MODE === '1' && process.env.TELEMETRY_SANITIZER_DISABLED === '1') return inner;
  return {
    export(spans, cb) {
      for (const s of spans) {
        try {
          s.attributes = sanitizeAttrs(s.attributes);
          s.name = scrub(s.name);
          for (const e of s.events || []) {
            e.attributes = e.name === 'exception'
              ? Object.fromEntries(Object.entries(e.attributes || {}).filter(([k]) => k === 'exception.type'))
              : sanitizeAttrs(e.attributes);
          }
          for (const l of s.links || []) l.attributes = sanitizeAttrs(l.attributes);
          if (s.status && s.status.message) s.status = { ...s.status, message: scrub(s.status.message).slice(0, 64) };
        } catch (_) {
          s.attributes = {}; s.events = []; s.links = [];
        }
      }
      inner.export(spans, cb);
    },
    shutdown() { return inner.shutdown(); },
    forceFlush() { return inner.forceFlush ? inner.forceFlush() : Promise.resolve(); },
  };
}

/** Copy allowlisted, schema-valid app.* baggage entries onto the active span (typed). */
function stampAppAttrs() {
  const api = require('@opentelemetry/api');
  const span = api.trace.getActiveSpan();
  const bag = api.propagation.getActiveBaggage();
  if (!span || !bag) return;
  for (const [key, entry] of Object.entries(bag.getAllEntries())) {
    if (!BAGGAGE_ALLOW.has(key)) continue;
    const value = key === 'app.customer.authenticated' ? entry.value === 'true' : entry.value;
    const f = APP_SCHEMA[key];
    if (f && f(value)) span.setAttribute(key, value);
  }
}

function setAppAttr(key, value) {
  const api = require('@opentelemetry/api');
  const span = api.trace.getActiveSpan();
  const f = APP_SCHEMA[key];
  if (span && f && f(value)) span.setAttribute(key, value);
}

module.exports = { initTelemetry, stampAppAttrs, setAppAttr, sanitizingExporter, sanitizeAttrs };
