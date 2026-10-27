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
	"encoding/json"
	"fmt"
	"net"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/redis/go-redis/v9"
	"go.opentelemetry.io/otel/attribute"

	"cloud.google.com/go/profiler"
	"github.com/google/uuid"
	"github.com/pkg/errors"
	"github.com/sirupsen/logrus"
	"google.golang.org/grpc"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/status"

	pb "github.com/GoogleCloudPlatform/microservices-demo/src/checkoutservice/genproto"
	money "github.com/GoogleCloudPlatform/microservices-demo/src/checkoutservice/money"
	healthpb "google.golang.org/grpc/health/grpc_health_v1"

	"go.opentelemetry.io/contrib/instrumentation/google.golang.org/grpc/otelgrpc"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/exporters/otlp/otlptrace/otlptracegrpc"
	"go.opentelemetry.io/otel/propagation"
	"go.opentelemetry.io/otel/sdk/resource"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
)

const (
	listenPort  = "5050"
	usdCurrency = "USD"
)

var log *logrus.Logger

func init() {
	log = logrus.New()
	log.Level = logrus.DebugLevel
	configureLogrus(log)
}

type checkoutService struct {
	productCatalogSvcAddr string
	productCatalogSvcConn *grpc.ClientConn

	cartSvcAddr string
	cartSvcConn *grpc.ClientConn

	currencySvcAddr string
	currencySvcConn *grpc.ClientConn

	shippingSvcAddr string
	shippingSvcConn *grpc.ClientConn

	emailSvcAddr string
	emailSvcConn *grpc.ClientConn

	paymentSvcAddr string
	paymentSvcConn *grpc.ClientConn

	// Durable checkout: the dedicated order-queue Redis, the accountservice client, and completions that
	// could not be published yet. queue == nil keeps the previous non-durable behaviour.
	queue          *orderQueue
	account        *accountClient
	accountSvcConn *grpc.ClientConn
	pending        pendingSet
}

func main() {
	ctx := context.Background()
	setupOtelLogging(log, "checkoutservice")

	if os.Getenv("ENABLE_TRACING") == "1" {
		log.Info("Tracing enabled.")
		initTracing()

	} else {
		log.Info("Tracing disabled.")
	}

	if os.Getenv("ENABLE_PROFILER") == "1" {
		log.Info("Profiling enabled.")
		go initProfiling("checkoutservice", "1.0.0")
	} else {
		log.Info("Profiling disabled.")
	}

	port := listenPort
	if os.Getenv("PORT") != "" {
		port = os.Getenv("PORT")
	}

	svc := new(checkoutService)
	mustMapEnv(&svc.shippingSvcAddr, "SHIPPING_SERVICE_ADDR")
	mustMapEnv(&svc.productCatalogSvcAddr, "PRODUCT_CATALOG_SERVICE_ADDR")
	mustMapEnv(&svc.cartSvcAddr, "CART_SERVICE_ADDR")
	mustMapEnv(&svc.currencySvcAddr, "CURRENCY_SERVICE_ADDR")
	mustMapEnv(&svc.emailSvcAddr, "EMAIL_SERVICE_ADDR")
	mustMapEnv(&svc.paymentSvcAddr, "PAYMENT_SERVICE_ADDR")

	mustConnGRPC(ctx, &svc.shippingSvcConn, svc.shippingSvcAddr)
	mustConnGRPC(ctx, &svc.productCatalogSvcConn, svc.productCatalogSvcAddr)
	mustConnGRPC(ctx, &svc.cartSvcConn, svc.cartSvcAddr)
	mustConnGRPC(ctx, &svc.currencySvcConn, svc.currencySvcAddr)
	mustConnGRPC(ctx, &svc.emailSvcConn, svc.emailSvcAddr)
	mustConnGRPC(ctx, &svc.paymentSvcConn, svc.paymentSvcAddr)

	svc.queue = newOrderQueue()
	if addr := os.Getenv("ACCOUNT_SERVICE_ADDR"); addr != "" {
		mustConnGRPC(ctx, &svc.accountSvcConn, addr)
		svc.account = newAccountClient(svc.accountSvcConn)
	}
	if svc.queue != nil {
		log.Info("durable idempotent checkout enabled (order queue configured)")
		go svc.runBackground(context.Background())
	} else {
		log.Warn("ORDER_QUEUE_ADDR not set: checkout is NOT durable or idempotent")
	}

	lis, err := net.Listen("tcp", fmt.Sprintf(":%s", port))
	if err != nil {
		log.Fatal(err)
	}

	var srv *grpc.Server

	// Propagate trace context always
	otel.SetTextMapPropagator(
		propagation.NewCompositeTextMapPropagator(
			propagation.TraceContext{}, allowlistBaggage{}))
	srv = grpc.NewServer(
		grpc.UnaryInterceptor(otelgrpc.UnaryServerInterceptor()),
		grpc.StreamInterceptor(otelgrpc.StreamServerInterceptor()),
	)

	pb.RegisterCheckoutServiceServer(srv, svc)
	healthpb.RegisterHealthServer(srv, svc)
	log.Infof("starting to listen on tcp: %q", lis.Addr().String())
	err = srv.Serve(lis)
	log.Fatal(err)
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

func (cs *checkoutService) Check(ctx context.Context, req *healthpb.HealthCheckRequest) (*healthpb.HealthCheckResponse, error) {
	return &healthpb.HealthCheckResponse{Status: healthpb.HealthCheckResponse_SERVING}, nil
}

func (cs *checkoutService) Watch(req *healthpb.HealthCheckRequest, ws healthpb.Health_WatchServer) error {
	return status.Errorf(codes.Unimplemented, "health check via Watch not implemented")
}

func (cs *checkoutService) placeOrderLegacy(ctx context.Context, req *pb.PlaceOrderRequest) (*pb.PlaceOrderResponse, error) {
	stampAppAttrs(ctx)
	log.WithContext(ctx).Info("[PlaceOrder] legacy path")

	orderID, err := uuid.NewUUID()
	if err != nil {
		return nil, status.Errorf(codes.Internal, "failed to generate order uuid")
	}
	// Inject the order id and customer identity into baggage so all
	// downstream spans in this PlaceOrder (payment, shipping, cart-empty,
	// email) carry them. There's no login/auth in this app — "customer"
	// here is the anonymous per-browser-session id (req.UserId) plus
	// whatever email/address the shopper entered on the checkout form, not
	// a persistent CRM-grade profile.
	ctx = withBaggage(ctx, "app.order.id", orderID.String())
	stampAppAttrs(ctx)

	prep, err := cs.prepareOrderItemsAndShippingQuoteFromCart(ctx, req.UserId, req.UserCurrency, req.Address)
	if err != nil {
		return nil, status.Errorf(codes.Internal, err.Error())
	}

	total := pb.Money{CurrencyCode: req.UserCurrency,
		Units: 0,
		Nanos: 0}
	total = money.Must(money.Sum(total, *prep.shippingCostLocalized))
	for _, it := range prep.orderItems {
		multPrice := money.MultiplySlow(*it.Cost, uint32(it.GetItem().GetQuantity()))
		total = money.Must(money.Sum(total, multPrice))
	}
	// Order-level dollar value and itemized contents (product names, not
	// just ids) — see stampOrderAttrs in otelattrs.go for why these are set
	// directly on the span instead of going through app.* baggage.
	stampOrderAttrs(ctx, total, prep.itemDescriptions)

	txID, err := cs.chargeCard(ctx, &total, req.CreditCard)
	if err != nil {
		return nil, status.Errorf(codes.Internal, "failed to charge card: %+v", err)
	}
	log.WithContext(ctx).Infof("payment went through (transaction_id: %s)", txID)
	// Inject the payment transaction id into baggage so shipping/email spans
	// carry app.payment.transaction_id.
	ctx = withBaggage(ctx, "app.payment.transaction_id", txID)

	shippingTrackingID, err := cs.shipOrder(ctx, req.Address, prep.cartItems)
	if err != nil {
		return nil, status.Errorf(codes.Unavailable, "shipping error: %+v", err)
	}
	// Inject the shipping tracking id into baggage so the email span carries
	// app.shipping.tracking_id.
	ctx = withBaggage(ctx, "app.shipping.tracking_id", shippingTrackingID)

	_ = cs.emptyUserCart(ctx, req.UserId)

	orderResult := &pb.OrderResult{
		OrderId:            orderID.String(),
		ShippingTrackingId: shippingTrackingID,
		ShippingCost:       prep.shippingCostLocalized,
		ShippingAddress:    req.Address,
		Items:              prep.orderItems,
	}

	if err := cs.sendOrderConfirmation(ctx, req.Email, orderResult); err != nil {
		log.WithContext(ctx).Warn("failed to send order confirmation")
	} else {
		log.WithContext(ctx).Info("order confirmation email sent")
	}
	resp := &pb.PlaceOrderResponse{Order: orderResult}
	return resp, nil
}

type orderPrep struct {
	orderItems            []*pb.OrderItem
	cartItems             []*pb.CartItem
	shippingCostLocalized *pb.Money
	// itemDescriptions is telemetry-only: human-readable "<name> x<qty> @
	// <unit price> <currency>" strings, one per line item. Built here
	// (rather than in PlaceOrder) because product names are only available
	// inside prepOrderItems's per-item product lookup, and are deliberately
	// NOT added to the pb.OrderItem proto itself — that would mean
	// regenerating genproto across every service that shares it, for a
	// field only telemetry needs.
	itemDescriptions []string
}

func (cs *checkoutService) prepareOrderItemsAndShippingQuoteFromCart(ctx context.Context, userID, userCurrency string, address *pb.Address) (orderPrep, error) {
	var out orderPrep
	cartItems, err := cs.getUserCart(ctx, userID)
	if err != nil {
		return out, fmt.Errorf("cart failure: %+v", err)
	}
	orderItems, itemDescriptions, err := cs.prepOrderItems(ctx, cartItems, userCurrency)
	if err != nil {
		return out, fmt.Errorf("failed to prepare order: %+v", err)
	}
	shippingUSD, err := cs.quoteShipping(ctx, address, cartItems)
	if err != nil {
		return out, fmt.Errorf("shipping quote failure: %+v", err)
	}
	shippingPrice, err := cs.convertCurrency(ctx, shippingUSD, userCurrency)
	if err != nil {
		return out, fmt.Errorf("failed to convert shipping cost to currency: %+v", err)
	}

	out.shippingCostLocalized = shippingPrice
	out.cartItems = cartItems
	out.orderItems = orderItems
	out.itemDescriptions = itemDescriptions
	return out, nil
}

func (cs *checkoutService) quoteShipping(ctx context.Context, address *pb.Address, items []*pb.CartItem) (*pb.Money, error) {
	shippingQuote, err := pb.NewShippingServiceClient(cs.shippingSvcConn).
		GetQuote(ctx, &pb.GetQuoteRequest{
			Address: address,
			Items:   items})
	if err != nil {
		return nil, fmt.Errorf("failed to get shipping quote: %+v", err)
	}
	return shippingQuote.GetCostUsd(), nil
}

func (cs *checkoutService) getUserCart(ctx context.Context, userID string) ([]*pb.CartItem, error) {
	cart, err := pb.NewCartServiceClient(cs.cartSvcConn).GetCart(ctx, &pb.GetCartRequest{UserId: userID})
	if err != nil {
		return nil, fmt.Errorf("failed to get user cart during checkout: %+v", err)
	}
	return cart.GetItems(), nil
}

func (cs *checkoutService) emptyUserCart(ctx context.Context, userID string) error {
	if _, err := pb.NewCartServiceClient(cs.cartSvcConn).EmptyCart(ctx, &pb.EmptyCartRequest{UserId: userID}); err != nil {
		return fmt.Errorf("failed to empty user cart during checkout: %+v", err)
	}
	return nil
}

func (cs *checkoutService) prepOrderItems(ctx context.Context, items []*pb.CartItem, userCurrency string) ([]*pb.OrderItem, []string, error) {
	out := make([]*pb.OrderItem, len(items))
	descriptions := make([]string, len(items))
	cl := pb.NewProductCatalogServiceClient(cs.productCatalogSvcConn)

	for i, item := range items {
		product, err := cl.GetProduct(ctx, &pb.GetProductRequest{Id: item.GetProductId()})
		if err != nil {
			return nil, nil, fmt.Errorf("failed to get product #%q", item.GetProductId())
		}
		price, err := cs.convertCurrency(ctx, product.GetPriceUsd(), userCurrency)
		if err != nil {
			return nil, nil, fmt.Errorf("failed to convert price of %q to %s", item.GetProductId(), userCurrency)
		}
		out[i] = &pb.OrderItem{
			Item: item,
			Cost: price}
		descriptions[i] = fmt.Sprintf("%s x%d @ %.2f %s", product.GetName(), item.GetQuantity(), moneyToFloat(*price), userCurrency)
	}
	return out, descriptions, nil
}

func (cs *checkoutService) convertCurrency(ctx context.Context, from *pb.Money, toCurrency string) (*pb.Money, error) {
	result, err := pb.NewCurrencyServiceClient(cs.currencySvcConn).Convert(ctx, &pb.CurrencyConversionRequest{
		From:   from,
		ToCode: toCurrency})
	if err != nil {
		return nil, fmt.Errorf("failed to convert currency: %+v", err)
	}
	return result, err
}

func (cs *checkoutService) chargeCard(ctx context.Context, amount *pb.Money, paymentInfo *pb.CreditCardInfo) (string, error) {
	paymentResp, err := pb.NewPaymentServiceClient(cs.paymentSvcConn).Charge(ctx, &pb.ChargeRequest{
		Amount:     amount,
		CreditCard: paymentInfo})
	if err != nil {
		return "", fmt.Errorf("could not charge the card: %+v", err)
	}
	return paymentResp.GetTransactionId(), nil
}

func (cs *checkoutService) sendOrderConfirmation(ctx context.Context, email string, order *pb.OrderResult) error {
	_, err := pb.NewEmailServiceClient(cs.emailSvcConn).SendOrderConfirmation(ctx, &pb.SendOrderConfirmationRequest{
		Email: email,
		Order: order})
	return err
}

func (cs *checkoutService) shipOrder(ctx context.Context, address *pb.Address, items []*pb.CartItem) (string, error) {
	resp, err := pb.NewShippingServiceClient(cs.shippingSvcConn).ShipOrder(ctx, &pb.ShipOrderRequest{
		Address: address,
		Items:   items})
	if err != nil {
		return "", fmt.Errorf("shipment failed: %+v", err)
	}
	return resp.GetTrackingId(), nil
}

func (cs *checkoutService) paymentClient() pb.PaymentServiceClient {
	return pb.NewPaymentServiceClient(cs.paymentSvcConn)
}

// runBackground retries completions that could not be published, and resumes checkouts whose worker
// died after the customer was charged (no card data is needed from PAYMENT_CHARGED onward).
func (cs *checkoutService) runBackground(ctx context.Context) {
	tick := time.NewTicker(2 * time.Second)
	janitor := time.NewTicker(janitorEvery())
	for {
		select {
		case <-tick.C:
			cs.retryPending(ctx)
		case <-janitor.C:
			cs.janitorOnce(ctx)
		}
	}
}

func (cs *checkoutService) retryPending(ctx context.Context) {
	for _, c := range cs.pending.snapshot() {
		cctx, span := checkoutTracer.Start(ctx, "orders.republish")
		_, err := cs.queue.advance(cctx, c.key, c.epoch, []string{stShipped}, stCompleted, c.msg, "completed", c.orderID, "resp", c.resp)
		c.tries++
		switch {
		case err == nil:
			cs.pending.done(c.key)
			setAppAttrs(span, attribute.String("app.order.outbox_status", "accepted"), attribute.Int("app.order.retry_count", c.tries))
		case err == errStale:
			cs.pending.done(c.key) // another worker (the janitor) owns it now
		default:
			setAppAttrs(span, attribute.Int("app.order.retry_count", c.tries))
		}
		span.End()
	}
}

var takeoverScript = redis.NewScript(`
local state = redis.call('HGET', KEYS[1], 'state')
if not state or state == 'COMPLETED' or state == 'PAYMENT_DECLINED' or state == 'FAILED_PRE_PAYMENT' or state == 'STARTED' then return {'SKIP'} end
local now = tonumber(ARGV[1])
if tonumber(redis.call('HGET', KEYS[1], 'lease_until')) > now then return {'SKIP'} end
local epoch = tonumber(redis.call('HGET', KEYS[1], 'epoch')) + 1
redis.call('HSET', KEYS[1], 'epoch', epoch, 'lease_until', now + tonumber(ARGV[2]), 'updated_ms', now)
return {'OK', epoch, state, redis.call('HGET', KEYS[1], 'plan') or '', redis.call('HGET', KEYS[1], 'order_id') or ''}
`)

func (cs *checkoutService) janitorOnce(ctx context.Context) {
	var cursor uint64
	for i := 0; i < 20; i++ {
		keys, next, err := cs.queue.rdb.Scan(ctx, cursor, checkoutKeyPref+"*", 100).Result()
		if err != nil {
			return
		}
		for _, k := range keys {
			res, err := takeoverScript.Run(ctx, cs.queue.rdb, []string{k}, nowMs(), checkoutLease.Milliseconds()).Slice()
			if err != nil || res[0].(string) != "OK" {
				continue
			}
			p := &plan{}
			if json.Unmarshal([]byte(res[3].(string)), p) != nil {
				continue
			}
			jctx, span := checkoutTracer.Start(ctx, "checkout.janitor.resume")
			key := strings.TrimPrefix(k, checkoutKeyPref)
			r := &run{cs: cs, key: key, epoch: res[1].(int64), orderID: res[4].(string), state: res[2].(string), plan: p, resumed: true}
			setAppAttrs(span, attribute.String("app.checkout.resume_from", r.state), attribute.Int("app.checkout.lease_epoch", int(r.epoch)))
			if _, derr := r.drive(jctx); derr != nil {
				span.SetAttributes(attribute.String("error.type", "resume_incomplete"))
			}
			span.End()
		}
		cursor = next
		if cursor == 0 {
			return
		}
	}
}

func janitorEvery() time.Duration {
	if os.Getenv("TELEMETRY_TEST_MODE") == "1" {
		if sec, err := strconv.Atoi(os.Getenv("CHECKOUT_JANITOR_SECONDS")); err == nil && sec > 0 {
			return time.Duration(sec) * time.Second
		}
	}
	return 10 * time.Second
}
