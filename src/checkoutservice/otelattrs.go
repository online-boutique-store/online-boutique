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

	pb "github.com/GoogleCloudPlatform/microservices-demo/src/checkoutservice/genproto"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/baggage"
	"go.opentelemetry.io/otel/trace"
)

// stampAppAttrs copies every app.* baggage entry onto the current span.
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

// moneyToFloat converts a pb.Money (whole units + fractional nanos, per the
// google.type.Money convention) to a plain float64, e.g. for use as a
// numeric OTel span attribute.
func moneyToFloat(m pb.Money) float64 {
	return float64(m.GetUnits()) + float64(m.GetNanos())/1e9
}

// stampOrderAttrs sets order-level dollar value and itemized contents
// directly on the current span. These are deliberately NOT routed through
// withBaggage/stampAppAttrs: that path always stores values as strings
// (see stampAppAttrs above), which would make app.order.total_value a text
// attribute instead of a real number — fine for display, useless for
// Observe-side aggregation (sum/avg revenue, etc). Setting them directly
// here keeps total_value numeric and items a proper string list.
//
// Trade-off: because this bypasses baggage, these attributes land only on
// this span (checkoutservice's PlaceOrder), not on every downstream span in
// the trace the way app.order.id/app.customer.* do. That's intentional —
// order value and contents are order-level facts, not something every
// child span (payment, shipping, email) needs to carry individually.
func stampOrderAttrs(ctx context.Context, total pb.Money, itemDescriptions []string) {
	span := trace.SpanFromContext(ctx)
	if !span.SpanContext().IsValid() {
		return
	}
	span.SetAttributes(
		attribute.Float64("app.order.total_value", moneyToFloat(total)),
		attribute.String("app.order.currency", total.GetCurrencyCode()),
		attribute.StringSlice("app.order.items", itemDescriptions),
	)
}
