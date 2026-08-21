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
