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
}
