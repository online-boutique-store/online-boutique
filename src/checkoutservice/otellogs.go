package main

import (
	"context"
	"io"
	"os"
	"strings"
	"time"

	"github.com/sirupsen/logrus"
	"go.opentelemetry.io/contrib/bridges/otellogrus"
	"go.opentelemetry.io/otel/exporters/otlp/otlplog/otlploggrpc"
	"go.opentelemetry.io/otel/log/global"
	sdklog "go.opentelemetry.io/otel/sdk/log"
	"go.opentelemetry.io/otel/sdk/resource"
	"go.opentelemetry.io/otel/trace"
)

func setupOtelLogging(logger *logrus.Logger, serviceName string) {
	if os.Getenv("ENABLE_TRACING") != "1" || os.Getenv("OTEL_LOGS_EXPORTER") != "otlp" {
		return
	}

	ctx, cancel := context.WithTimeout(context.Background(), 3*time.Second)
	defer cancel()

	exporter, err := newLogExporter(ctx)
	if err != nil {
		logrus.Warnf("failed to create OTLP log exporter: %v", err)
		return
	}

	res, err := resource.New(ctx, resource.WithFromEnv(), resource.WithTelemetrySDK())
	if err != nil {
		logrus.Warnf("failed to create log resource: %v", err)
	}

	provider := sdklog.NewLoggerProvider(
		sdklog.WithResource(res),
		sdklog.WithProcessor(sdklog.NewBatchProcessor(exporter)),
	)
	global.SetLoggerProvider(provider)

	logger.AddHook(otellogrus.NewHook(serviceName, otellogrus.WithLoggerProvider(provider)))
	logger.SetOutput(io.Discard)
}

func newLogExporter(ctx context.Context) (*otlploggrpc.Exporter, error) {
	endpoint := strings.TrimPrefix(os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT"), "http://")
	endpoint = strings.TrimPrefix(endpoint, "https://")
	if endpoint == "" {
		endpoint = os.Getenv("COLLECTOR_SERVICE_ADDR")
	}
	if endpoint == "" {
		endpoint = "localhost:4317"
	}

	return otlploggrpc.New(ctx,
		otlploggrpc.WithEndpoint(endpoint),
		otlploggrpc.WithInsecure(),
	)
}

func configureLogrus(logger *logrus.Logger) {
	logger.SetFormatter(&logrus.JSONFormatter{
		FieldMap: logrus.FieldMap{
			logrus.FieldKeyTime:  "timestamp",
			logrus.FieldKeyLevel: "severity",
			logrus.FieldKeyMsg:   "message",
		},
		TimestampFormat: time.RFC3339Nano,
	})
	logger.SetOutput(os.Stdout)
	// Stamp trace_id/span_id onto every stdout log line, independent of
	// whether the OTLP log exporter below is enabled. In every current
	// deployment OTEL_LOGS_EXPORTER=none, so setupOtelLogging is a no-op and
	// stdout JSON (this formatter) is what actually reaches Observe — this
	// hook is what makes those lines correlate to a trace.
	logger.AddHook(traceContextHook{})
}

// traceContextHook reads the active span from the log entry's context
// (set via logrus's .WithContext(ctx)) and adds trace_id/span_id fields so
// every log line can be pivoted to its trace in Observe. Entries with no
// context, or a context with no recording span (e.g. startup/background
// logs before any request comes in), are left unchanged.
type traceContextHook struct{}

func (traceContextHook) Levels() []logrus.Level {
	return logrus.AllLevels
}

func (traceContextHook) Fire(entry *logrus.Entry) error {
	if entry.Context == nil {
		return nil
	}
	sc := trace.SpanContextFromContext(entry.Context)
	if !sc.IsValid() {
		return nil
	}
	entry.Data["trace_id"] = sc.TraceID().String()
	entry.Data["span_id"] = sc.SpanID().String()
	return nil
}
