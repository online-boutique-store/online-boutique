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
	"strings"

	pb "github.com/GoogleCloudPlatform/microservices-demo/src/frontend/genproto"
	"github.com/GoogleCloudPlatform/microservices-demo/src/frontend/money"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/baggage"
	"go.opentelemetry.io/otel/trace"
)

// stampAppAttrs copies every app.* baggage entry onto the current span.
// Call this at the entry of every handler so each span carries all
// attributes that exist at that point in the request flow.
func stampAppAttrs(ctx context.Context) {
	span := trace.SpanFromContext(ctx)
	if !span.SpanContext().IsValid() {
		return
	}
	for _, m := range baggage.FromContext(ctx).Members() {
		if strings.HasPrefix(m.Key(), "app.") {
			span.SetAttributes(attribute.String(m.Key(), m.Value()))
		}
	}
}

// withBaggage returns a context with the given app.* key/value added to the
// baggage, so downstream gRPC calls (which use the same context) propagate it.
func withBaggage(ctx context.Context, key, value string) context.Context {
	if value == "" {
		return ctx
	}
	m, err := baggage.NewMember(key, value)
	if err != nil {
		return ctx
	}
	bag, err := baggage.FromContext(ctx).SetMember(m)
	if err != nil {
		return ctx
	}
	return baggage.ContextWithBaggage(ctx, bag)
}

// withBaggageSlice is like withBaggage but joins a slice into a single
// comma-separated string (baggage values are strings).
func withBaggageSlice(ctx context.Context, key string, values []string) context.Context {
	return withBaggage(ctx, key, strings.Join(values, ","))
}

// moneyToFloat converts a pb.Money (whole units + fractional nanos, per the
// google.type.Money convention) to a plain float64.
func moneyToFloat(m pb.Money) float64 {
	return float64(m.GetUnits()) + float64(m.GetNanos())/1e9
}

// stampCartValueAttrs sets the pre-checkout cart's dollar value directly on
// the current span. Deliberately NOT routed through withBaggage/
// stampAppAttrs — that path always stores values as strings (fine for
// display, useless for Observe-side numeric aggregation like average cart
// value). Call this once the cart total has been computed, on the cart-view
// handler's span.
func stampCartValueAttrs(ctx context.Context, total pb.Money, itemCount int) {
	span := trace.SpanFromContext(ctx)
	if !span.SpanContext().IsValid() {
		return
	}
	span.SetAttributes(
		attribute.Float64("app.cart.total_value", moneyToFloat(total)),
		attribute.String("app.cart.currency", total.GetCurrencyCode()),
		attribute.Int("app.cart.item_count", itemCount),
	)
}

// stampFailedCheckoutValue is called when a checkout attempt errors out. It
// re-fetches the user's cart and independently recomputes its dollar value,
// so Observe can show not just that checkout failed, but how much revenue a
// given incident window blocked. Best-effort: if the cart/product/currency
// lookups themselves fail, it returns without attaching partial or
// misleading numbers.
func (fe *frontendServer) stampFailedCheckoutValue(ctx context.Context, userID, currency string) {
	span := trace.SpanFromContext(ctx)
	if !span.SpanContext().IsValid() {
		return
	}

	cart, err := fe.getCart(ctx, userID)
	if err != nil {
		return
	}

	total := pb.Money{CurrencyCode: currency}
	for _, item := range cart {
		product, err := fe.getProduct(ctx, item.GetProductId())
		if err != nil {
			return
		}
		price, err := fe.convertCurrency(ctx, product.GetPriceUsd(), currency)
		if err != nil {
			return
		}
		multPrice := money.MultiplySlow(*price, uint32(item.GetQuantity()))
		total = money.Must(money.Sum(total, multPrice))
	}

	span.SetAttributes(
		attribute.Bool("app.checkout.failed", true),
		attribute.Float64("app.checkout.failed_cart_value", moneyToFloat(total)),
		attribute.String("app.checkout.failed_cart_currency", currency),
		attribute.Int("app.checkout.failed_cart_item_count", len(cart)),
	)
}
