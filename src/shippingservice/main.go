// Copyright 2018 Google LLC
//
// Licensed under the Apache License, Version 2.0 (the "License");
// you may not use this file except in compliance with the License.
// You may obtain a copy of the License at
//
//      http://www.apache.org/licenses/LICENSE-2.0
//
// Unless required by applicable law or agreed to in writing, software
// distributed under the License is distributed on an "AS IS" BASIS,
// WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
// See the License for the specific language governing permissions and
// limitations under the License.

package main

import (
	"context"
	"fmt"
	"net"
	"os"
	"time"

	"cloud.google.com/go/profiler"
	"github.com/pkg/errors"
	"github.com/sirupsen/logrus"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/credentials/insecure"
	"google.golang.org/grpc/reflection"
	"google.golang.org/grpc/status"

	pb "github.com/GoogleCloudPlatform/microservices-demo/src/shippingservice/genproto"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"

	"go.opentelemetry.io/contrib/instrumentation/google.golang.org/grpc/otelgrpc"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/trace"
)

const (
	defaultPort = "50051"
)

var log *logrus.Logger

func init() {
	log = logrus.New()
	log.Level = logrus.DebugLevel
	configureLogrus(log)
}

func main() {
	setupOtelLogging(log, "shippingservice")

	if os.Getenv("ENABLE_TRACING") == "1" {
		log.Info("Tracing enabled.")
		initTracing()
	} else {
		log.Info("Tracing disabled.")
	}

	if os.Getenv("DISABLE_PROFILER") == "" {
		log.Info("Profiling enabled.")
		go initProfiling("shippingservice", "1.0.0")
	} else {
		log.Info("Profiling disabled.")
	}

	port := defaultPort
	if value, ok := os.LookupEnv("PORT"); ok {
		port = value
	}
	port = fmt.Sprintf(":%s", port)

	lis, err := net.Listen("tcp", port)
	if err != nil {
		log.Fatalf("failed to listen: %v", err)
	}

	otel.SetTextMapPropagator(
		propagation.NewCompositeTextMapPropagator(
			propagation.TraceContext{}, allowlistBaggage{}))
	srv := grpc.NewServer(
		grpc.UnaryInterceptor(otelgrpc.UnaryServerInterceptor()),
		grpc.StreamInterceptor(otelgrpc.StreamServerInterceptor()),
	)
	svc := &server{store: newShipmentStore()}
	pb.RegisterShippingServiceServer(srv, svc)
	healthpb.RegisterHealthServer(srv, svc)
	log.Infof("Shipping Service listening on port %s", port)

	// Register reflection service on gRPC server.
	reflection.Register(srv)
	if err := srv.Serve(lis); err != nil {
		log.Fatalf("failed to serve: %v", err)
	}
}

// server controls RPC service responses.
type server struct{ store *shipmentStore }

// Check is for health checking.
func (s *server) Check(ctx context.Context, req *healthpb.HealthCheckRequest) (*healthpb.HealthCheckResponse, error) {
	return &healthpb.HealthCheckResponse{Status: healthpb.HealthCheckResponse_SERVING}, nil
}

func (s *server) Watch(req *healthpb.HealthCheckRequest, ws healthpb.Health_WatchServer) error {
	return status.Errorf(codes.Unimplemented, "health check via Watch not implemented")
}

// GetQuote produces a shipping quote (cost) in USD.
func (s *server) GetQuote(ctx context.Context, in *pb.GetQuoteRequest) (*pb.GetQuoteResponse, error) {
	stampAppAttrs(ctx)
	log.WithContext(ctx).Info("[GetQuote] received request")
	defer log.WithContext(ctx).Info("[GetQuote] completed request")

	// 1. Generate a quote based on the total number of items to be shipped.
	quote := CreateQuoteFromCount(0)

	// 2. Generate a response.
	return &pb.GetQuoteResponse{
		CostUsd: &pb.Money{
			CurrencyCode: "USD",
			Units:        int64(quote.Dollars),
			Nanos:        int32(quote.Cents * 10000000)},
	}, nil

}

// ShipOrder mocks that the requested items will be shipped.
// It supplies a tracking ID for notional lookup of shipment delivery status. With an idempotency
// key the shipment is recorded durably first: a replay (client retry, crash and restart) returns
// the ORIGINAL tracking id and never creates a second shipment. If the record cannot be written
// the call fails (UNAVAILABLE) instead of shipping without a durable record.
func (s *server) ShipOrder(ctx context.Context, in *pb.ShipOrderRequest) (*pb.ShipOrderResponse, error) {
	stampAppAttrs(ctx)
	log.WithContext(ctx).Info("[ShipOrder] received request")
	defer log.WithContext(ctx).Info("[ShipOrder] completed request")
	span := trace.SpanFromContext(ctx)
	key := in.GetIdempotencyKey()
	var id string
	replayed := false
	if key != "" {
		if s.store == nil {
			return nil, status.Error(codes.FailedPrecondition, "idempotent shipping requires ORDER_QUEUE_ADDR")
		}
		var err error
		id, replayed, err = s.store.record(ctx, key, deterministicTrackingID(key))
		if err != nil {
			return nil, status.Error(codes.Unavailable, "shipment_record_unavailable")
		}
		if testFault() == "exit_after_record" && !replayed {
			os.Exit(1) // test-only: the shipment is durably recorded but the caller never hears back
		}
	} else {
		baseAddress := fmt.Sprintf("%s, %s, %s", in.Address.StreetAddress, in.Address.City, in.Address.State)
		id = CreateTrackingId(baseAddress)
	}
	if span.SpanContext().IsValid() {
		span.SetAttributes(attribute.String("app.shipping.tracking_id", id), attribute.Bool("app.shipping.replayed", replayed))
	}
	return &pb.ShipOrderResponse{TrackingId: id, Replayed: replayed}, nil
}

// GetShipment reports whether a shipment exists for an idempotency key (used to resolve an uncertain ShipOrder).
func (s *server) GetShipment(ctx context.Context, in *pb.GetShipmentRequest) (*pb.GetShipmentResponse, error) {
	if s.store == nil || in.GetIdempotencyKey() == "" {
		return nil, status.Error(codes.FailedPrecondition, "idempotent shipping not configured")
	}
	id, found, err := s.store.get(ctx, in.GetIdempotencyKey())
	if err != nil {
		return nil, status.Error(codes.Unavailable, "shipment_record_unavailable")
	}
	return &pb.GetShipmentResponse{Found: found, TrackingId: id}, nil
}

func initStats() {
	//TODO(arbrown) Implement OpenTelemetry stats
}

func initTracing() {
	var (
		collectorAddr string
	)

	ctx := context.Background()
	ctx, cancel := context.WithTimeout(ctx, time.Second*3)
	defer cancel()

	mustMapEnv(&collectorAddr, "COLLECTOR_SERVICE_ADDR")
	// The exporter's own connection must not be instrumented, or every export creates more spans.
	collectorConn, err := grpc.DialContext(ctx, collectorAddr, grpc.WithInsecure())
	if err != nil {
		panic(errors.Wrapf(err, "grpc: failed to connect %s", collectorAddr))
	}

	exporter, err := otlptracegrpc.New(
		ctx,
		otlptracegrpc.WithGRPCConn(collectorConn))
	if err != nil {
		log.Warnf("warn: Failed to create trace exporter: %v", err)
		return
	}
	res, err := resource.New(ctx,
		resource.WithFromEnv(),
		resource.WithTelemetrySDK(),
	)
	if err != nil {
		log.Warnf("warn: Failed to create trace resource: %v", err)
	}
	tp := sdktrace.NewTracerProvider(
		sdktrace.WithBatcher(sanitizingExporter{inner: exporter}),
		sdktrace.WithResource(res),
		sdktrace.WithSampler(sdktrace.AlwaysSample()))
	otel.SetTracerProvider(tp)
}

func mustMapEnv(target *string, envKey string) {
	v := os.Getenv(envKey)
	if v == "" {
		panic(fmt.Sprintf("environment variable %q not set", envKey))
	}
	*target = v
}

func mustConnGRPC(ctx context.Context, conn **grpc.ClientConn, addr string) {
	var err error
	ctx, cancel := context.WithTimeout(ctx, time.Second*3)
	defer cancel()
	*conn, err = grpc.DialContext(ctx, addr,
		grpc.WithTransportCredentials(insecure.NewCredentials()),
		grpc.WithUnaryInterceptor(otelgrpc.UnaryClientInterceptor()),
		grpc.WithStreamInterceptor(otelgrpc.StreamClientInterceptor()))
	if err != nil {
		panic(errors.Wrapf(err, "grpc: failed to connect %s", addr))
	}
}

func initProfiling(service, version string) {
	// TODO(ahmetb) this method is duplicated in other microservices using Go
	// since they are not sharing packages.
	for i := 1; i <= 3; i++ {
		if err := profiler.Start(profiler.Config{
			Service:        service,
			ServiceVersion: version,
			// ProjectID must be set if not running on GCP.
			// ProjectID: "my-project",
		}); err != nil {
			log.Warnf("failed to start profiler: %+v", err)
		} else {
			log.Info("started Stackdriver profiler")
			return
		}
		d := time.Second * 10 * time.Duration(i)
		log.Infof("sleeping %v to retry initializing Stackdriver profiler", d)
		time.Sleep(d)
	}
	log.Warn("could not initialize Stackdriver profiler after retrying, giving up")
}
