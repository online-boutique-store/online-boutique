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
	"net/http"
	"os"
	"time"

	"cloud.google.com/go/profiler"
	"github.com/gorilla/mux"
	"github.com/pkg/errors"
	"github.com/sirupsen/logrus"
	"go.opentelemetry.io/contrib/instrumentation/google.golang.org/grpc/otelgrpc"
	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"google.golang.org/grpc"
)

const (
	port            = "8080"
	defaultCurrency = "USD"
	cookieMaxAge    = 60 * 60 * 48

	cookiePrefix    = "shop_"
	cookieSessionID = cookiePrefix + "session-id"
	cookieCurrency  = cookiePrefix + "currency"
	cookieAuth      = cookiePrefix + "account-session"
	cookieCSRF      = cookiePrefix + "csrf"
)

var (
	whitelistedCurrencies = map[string]bool{
		"USD": true,
		"EUR": true,
		"CAD": true,
		"JPY": true,
		"GBP": true,
		"TRY": true,
	}

	baseUrl = ""
)

type ctxKeySessionID struct{}

type frontendServer struct {
	productCatalogSvcAddr string
	productCatalogSvcConn *grpc.ClientConn

	currencySvcAddr string
	currencySvcConn *grpc.ClientConn

	cartSvcAddr string
	cartSvcConn *grpc.ClientConn

	recommendationSvcAddr string
	recommendationSvcConn *grpc.ClientConn

	checkoutSvcAddr string
	checkoutSvcConn *grpc.ClientConn

	shippingSvcAddr string
	shippingSvcConn *grpc.ClientConn

	adSvcAddr string
	adSvcConn *grpc.ClientConn

	collectorAddr string
	collectorConn *grpc.ClientConn

	shoppingAssistantSvcAddr string
	accountAPISecret         string

	// ACCOUNT_BACKEND=grpc: accountservice owns accounts; account == nil means the legacy local flow.
	account        *accountGRPC
	accountSvcAddr string
	accountSvcConn *grpc.ClientConn
}

func main() {
	ctx := context.Background()
	log := logrus.New()
	log.Level = logrus.DebugLevel
	configureLogrus(log)
	setupOtelLogging(log, "frontend")

	svc := new(frontendServer)

	otel.SetTextMapPropagator(
		propagation.NewCompositeTextMapPropagator(
			propagation.TraceContext{}, allowlistBaggage{}))

	baseUrl = os.Getenv("BASE_URL")

	if os.Getenv("ENABLE_TRACING") == "1" {
		log.Info("Tracing enabled.")
		initTracing(log, ctx, svc)
	} else {
		log.Info("Tracing disabled.")
	}

	if os.Getenv("ENABLE_PROFILER") == "1" {
		log.Info("Profiling enabled.")
		go initProfiling(log, "frontend", "1.0.0")
	} else {
		log.Info("Profiling disabled.")
	}

	srvPort := port
	if os.Getenv("PORT") != "" {
		srvPort = os.Getenv("PORT")
	}
	addr := os.Getenv("LISTEN_ADDR")
	mustMapEnv(&svc.productCatalogSvcAddr, "PRODUCT_CATALOG_SERVICE_ADDR")
	mustMapEnv(&svc.currencySvcAddr, "CURRENCY_SERVICE_ADDR")
	mustMapEnv(&svc.cartSvcAddr, "CART_SERVICE_ADDR")
	mustMapEnv(&svc.recommendationSvcAddr, "RECOMMENDATION_SERVICE_ADDR")
	mustMapEnv(&svc.checkoutSvcAddr, "CHECKOUT_SERVICE_ADDR")
	mustMapEnv(&svc.shippingSvcAddr, "SHIPPING_SERVICE_ADDR")
	mustMapEnv(&svc.adSvcAddr, "AD_SERVICE_ADDR")
	mustMapEnv(&svc.shoppingAssistantSvcAddr, "SHOPPING_ASSISTANT_SERVICE_ADDR")
	if assistantEnabled {
		mustMapEnv(&svc.accountAPISecret, "ACCOUNT_API_SECRET")
	}

	if os.Getenv("ACCOUNT_BACKEND") == "grpc" {
		var accountSecret string
		mustMapEnv(&svc.accountSvcAddr, "ACCOUNT_SERVICE_ADDR")
		mustMapEnv(&accountSecret, "ACCOUNT_SERVICE_SECRET")
		mustConnGRPC(ctx, &svc.accountSvcConn, svc.accountSvcAddr)
		svc.account = newAccountGRPC(svc.accountSvcConn, accountSecret)
		accountDashboardEnabled = true
		log.Info("account backend: accountservice (gRPC)")
	} else {
		log.Info("account backend: local (shopping assistant HTTP API)")
	}

	mustConnGRPC(ctx, &svc.currencySvcConn, svc.currencySvcAddr)
	mustConnGRPC(ctx, &svc.productCatalogSvcConn, svc.productCatalogSvcAddr)
	mustConnGRPC(ctx, &svc.cartSvcConn, svc.cartSvcAddr)
	mustConnGRPC(ctx, &svc.recommendationSvcConn, svc.recommendationSvcAddr)
	mustConnGRPC(ctx, &svc.shippingSvcConn, svc.shippingSvcAddr)
	mustConnGRPC(ctx, &svc.checkoutSvcConn, svc.checkoutSvcAddr)
	mustConnGRPC(ctx, &svc.adSvcConn, svc.adSvcAddr)

	r := mux.NewRouter()
	r.HandleFunc(baseUrl+"/", svc.homeHandler).Methods(http.MethodGet, http.MethodHead)
	r.HandleFunc(baseUrl+"/product/{id}", svc.productHandler).Methods(http.MethodGet, http.MethodHead)
	r.HandleFunc(baseUrl+"/cart", svc.viewCartHandler).Methods(http.MethodGet, http.MethodHead)
	r.HandleFunc(baseUrl+"/cart", svc.addToCartHandler).Methods(http.MethodPost)
	r.HandleFunc(baseUrl+"/cart/empty", svc.emptyCartHandler).Methods(http.MethodPost)
	r.HandleFunc(baseUrl+"/setCurrency", svc.setCurrencyHandler).Methods(http.MethodPost)
	r.HandleFunc(baseUrl+"/account/create", svc.accountCreateHandler).Methods(http.MethodGet, http.MethodPost)
	r.HandleFunc(baseUrl+"/account/activation", svc.accountActivationHandler).Methods(http.MethodGet, http.MethodPost)
	r.HandleFunc(baseUrl+"/cart/checkout", svc.placeOrderHandler).Methods(http.MethodPost)
	r.HandleFunc(baseUrl+"/account/profile", svc.accountProfileHandler).Methods(http.MethodGet, http.MethodPost)
	r.HandleFunc(baseUrl+"/login", svc.loginHandler).Methods(http.MethodGet, http.MethodPost)
	r.HandleFunc(baseUrl+"/logout", svc.logoutHandler).Methods(http.MethodGet, http.MethodPost)
	if svc.account != nil {
		r.HandleFunc(baseUrl+"/account", svc.requireLogin(svc.accountDashboard)).Methods(http.MethodGet, http.MethodHead)
		r.HandleFunc(baseUrl+"/account/addresses", svc.requireLogin(svc.accountAddresses)).Methods(http.MethodGet, http.MethodHead)
		r.HandleFunc(baseUrl+"/account/addresses/new", svc.requireLogin(svc.accountAddressForm)).Methods(http.MethodGet, http.MethodPost)
		r.HandleFunc(baseUrl+"/account/addresses/{id}/edit", svc.requireLogin(svc.accountAddressForm)).Methods(http.MethodGet, http.MethodPost)
		r.HandleFunc(baseUrl+"/account/addresses/{id}/delete", svc.requireLogin(svc.accountAddressAction("delete"))).Methods(http.MethodPost)
		r.HandleFunc(baseUrl+"/account/addresses/{id}/default", svc.requireLogin(svc.accountAddressAction("default"))).Methods(http.MethodPost)
		r.HandleFunc(baseUrl+"/account/orders", svc.requireLogin(svc.accountOrders)).Methods(http.MethodGet, http.MethodHead)
		r.HandleFunc(baseUrl+"/account/orders/{id}", svc.requireLogin(svc.accountOrder)).Methods(http.MethodGet, http.MethodHead)
		r.HandleFunc(baseUrl+"/account/payment-methods", svc.requireLogin(svc.accountPayments)).Methods(http.MethodGet, http.MethodHead)
		r.HandleFunc(baseUrl+"/account/payment-methods/add", svc.requireLogin(svc.accountPaymentAdd)).Methods(http.MethodPost)
		r.HandleFunc(baseUrl+"/account/payment-methods/{id}/delete", svc.requireLogin(svc.accountPaymentAction("delete"))).Methods(http.MethodPost)
		r.HandleFunc(baseUrl+"/account/payment-methods/{id}/default", svc.requireLogin(svc.accountPaymentAction("default"))).Methods(http.MethodPost)
	}
	r.HandleFunc(baseUrl+"/assistant", svc.assistantHandler).Methods(http.MethodGet)
	r.PathPrefix(baseUrl + "/static/").Handler(http.StripPrefix(baseUrl+"/static/", http.FileServer(http.Dir("./static/"))))
	r.HandleFunc(baseUrl+"/robots.txt", func(w http.ResponseWriter, _ *http.Request) { fmt.Fprint(w, "User-agent: *\nDisallow: /") })
	r.HandleFunc(baseUrl+"/_healthz", func(w http.ResponseWriter, _ *http.Request) { fmt.Fprint(w, "ok") })
	r.HandleFunc(baseUrl+"/product-meta/{ids}", svc.getProductByID).Methods(http.MethodGet)
	r.HandleFunc(baseUrl+"/bot", svc.chatBotHandler).Methods(http.MethodPost)

	var handler http.Handler = r
	handler = svc.accountContext(handler)              // resolve account session, seed identity baggage
	handler = &logHandler{log: log, next: handler}     // add logging
	handler = ensureSessionID(handler)                 // add session ID
	handler = otelhttp.NewHandler(handler, "frontend") // add OTel tracing

	log.Infof("starting server on " + addr + ":" + srvPort)
	log.Fatal(http.ListenAndServe(addr+":"+srvPort, handler))
}
func initStats(log logrus.FieldLogger) {
	// TODO(arbrown) Implement OpenTelemtry stats
}

func initTracing(log logrus.FieldLogger, ctx context.Context, svc *frontendServer) (*sdktrace.TracerProvider, error) {
	mustMapEnv(&svc.collectorAddr, "COLLECTOR_SERVICE_ADDR")
	// Plain (uninstrumented) connection so exporting spans is not itself traced.
	dialCtx, cancel := context.WithTimeout(ctx, time.Second*3)
	defer cancel()
	exportConn, err := grpc.DialContext(dialCtx, svc.collectorAddr, grpc.WithInsecure())
	if err != nil {
		panic(errors.Wrapf(err, "grpc: failed to connect %s", svc.collectorAddr))
	}
	svc.collectorConn = exportConn
	exporter, err := otlptracegrpc.New(
		ctx,
		otlptracegrpc.WithGRPCConn(exportConn))
	if err != nil {
		log.Warnf("warn: Failed to create trace exporter: %v", err)
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

	return tp, err
}

func initProfiling(log logrus.FieldLogger, service, version string) {
	// TODO(ahmetb) this method is duplicated in other microservices using Go
	// since they are not sharing packages.
	for i := 1; i <= 3; i++ {
		log = log.WithField("retry", i)
		if err := profiler.Start(profiler.Config{
			Service:        service,
			ServiceVersion: version,
			// ProjectID must be set if not running on GCP.
			// ProjectID: "my-project",
		}); err != nil {
			log.Warnf("warn: failed to start profiler: %+v", err)
		} else {
			log.Info("started Stackdriver profiler")
			return
		}
		d := time.Second * 10 * time.Duration(i)
		log.Debugf("sleeping %v to retry initializing Stackdriver profiler", d)
		time.Sleep(d)
	}
	log.Warn("warning: could not initialize Stackdriver profiler after retrying, giving up")
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
		grpc.WithInsecure(),
		grpc.WithUnaryInterceptor(otelgrpc.UnaryClientInterceptor()),
		grpc.WithStreamInterceptor(otelgrpc.StreamClientInterceptor()))
	if err != nil {
		panic(errors.Wrapf(err, "grpc: failed to connect %s", addr))
	}
}
