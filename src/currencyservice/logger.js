'use strict';

const { trace, context } = require('@opentelemetry/api');

function otlpLogsEnabled() {
  return process.env.ENABLE_TRACING === '1'
    && process.env.OTEL_LOGS_EXPORTER === 'otlp';
}

function correlationAttributes() {
  const fields = {};
  const pod = process.env.K8S_POD_NAME;
  const ns = process.env.K8S_NAMESPACE;
  if (pod) {
    fields['k8s.pod.name'] = pod;
  }
  if (ns) {
    fields['k8s.namespace.name'] = ns;
  }

  const span = trace.getActiveSpan();
  if (!span) {
    return fields;
  }

  const ctx = span.spanContext();
  if (ctx.traceId) {
    fields.trace_id = ctx.traceId;
  }
  if (ctx.spanId) {
    fields.span_id = ctx.spanId;
  }
  return fields;
}

function createOtelLogger(name) {
  const { logs, SeverityNumber } = require('@opentelemetry/api-logs');
  const otelLogger = logs.getLogger(name);

  const severity = {
    trace: SeverityNumber.TRACE,
    debug: SeverityNumber.DEBUG,
    info: SeverityNumber.INFO,
    warn: SeverityNumber.WARN,
    error: SeverityNumber.ERROR,
    fatal: SeverityNumber.FATAL,
  };

  function emit(level, msg, extra) {
    const attributes = { ...correlationAttributes() };
    if (extra && typeof extra === 'object') {
      Object.assign(attributes, extra);
    }
    delete attributes.trace_id;
    delete attributes.span_id;
    otelLogger.emit(
      {
        severityNumber: severity[level] || SeverityNumber.INFO,
        severityText: level,
        body: typeof msg === 'string' ? msg : String(msg),
        attributes,
      },
      context.active(),
    );
  }

  return {
    info: (msg, extra) => emit('info', msg, extra),
    warn: (msg, extra) => emit('warn', msg, extra),
    error: (msg, extra) => emit('error', msg, extra),
    debug: (msg, extra) => emit('debug', msg, extra),
  };
}

function createPinoLogger(name) {
  const pino = require('pino');
  return pino({
    name,
    messageKey: 'message',
    mixin: correlationAttributes,
    formatters: {
      level(logLevelString) {
        return { severity: logLevelString };
      },
    },
  });
}

function createLogger(name) {
  if (otlpLogsEnabled()) {
    return createOtelLogger(name);
  }
  return createPinoLogger(name);
}

module.exports = { createLogger };
