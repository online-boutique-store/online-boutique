package main

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"os"
	"regexp"
	"strconv"
	"strings"

	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/baggage"
	"go.opentelemetry.io/otel/propagation"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	"go.opentelemetry.io/otel/trace"
)

// ---- opaque references ---------------------------------------------------------------------

var telemetryRefKey = os.Getenv("TELEMETRY_REF_KEY")

// sessionRef returns the opaque, stable reference exported in place of the raw browser session
// id (HMAC-SHA256, 16 hex chars). Empty when no key is configured: the attribute is then omitted.
func sessionRef(id string) string {
	if id == "" || telemetryRefKey == "" {
		return ""
	}
	m := hmac.New(sha256.New, []byte(telemetryRefKey))
	m.Write([]byte(id))
	return hex.EncodeToString(m.Sum(nil))[:16]
}

// ---- typed business attributes ------------------------------------------------------------

var (
	reUUID    = regexp.MustCompile(`^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$`)
	reRef     = regexp.MustCompile(`^[0-9a-f]{16}$`)
	reCode    = regexp.MustCompile(`^[a-z0-9_.]{1,48}$`)
	reSafeID  = regexp.MustCompile(`^[A-Za-z0-9._:-]{1,64}$`)
	reCurrncy = regexp.MustCompile(`^[A-Z]{3}$`)
)

func enumOf(vals ...string) func(attribute.Value) bool {
	set := map[string]bool{}
	for _, v := range vals {
		set[v] = true
	}
	return func(v attribute.Value) bool { return v.Type() == attribute.STRING && set[v.AsString()] }
}
func regexOf(re *regexp.Regexp) func(attribute.Value) bool {
	return func(v attribute.Value) bool { return v.Type() == attribute.STRING && re.MatchString(v.AsString()) }
}
func boolOf(v attribute.Value) bool { return v.Type() == attribute.BOOL }
func intIn(lo, hi int64) func(attribute.Value) bool {
	return func(v attribute.Value) bool {
		return v.Type() == attribute.INT64 && v.AsInt64() >= lo && v.AsInt64() <= hi
	}
}

// appSchema mirrors the relevant slice of accountservice/telemetry_schema.py. Any app.* attribute
// not listed here or in legacyAppKeys is dropped at export (fail closed).
var appSchema = map[string]func(attribute.Value) bool{
	"app.customer.id":                  regexOf(reUUID),
	"app.customer.authenticated":       boolOf,
	"app.customer.type":                enumOf("authenticated", "guest", "anonymous"),
	"app.customer.profile_complete":    boolOf,
	"app.account.action":               enumOf("register", "activate", "login", "logout", "profile_update", "address_create", "address_update", "address_delete", "address_default", "payment_add", "payment_delete", "payment_default", "orders_list", "order_view", "dashboard_view", "claim_guest_orders", "validate", "session_resolve", "profile_get", "address_list", "payment_list", "login_redirect"),
	"app.account.profile_version":      intIn(1, 2),
	"app.account.outcome":              regexOf(reCode),
	"app.account.guest_claim":          enumOf("failed"),
	"app.account.guest_orders_claimed": intIn(0, 100000),
	"app.account.address_count":        intIn(0, 100),
	"app.account.payment_method_count": intIn(0, 100),
	"app.account.panel_failures":       intIn(0, 10),
	"app.validation.fields":            func(v attribute.Value) bool { return v.Type() == attribute.STRINGSLICE },
	"app.validation.codes":             func(v attribute.Value) bool { return v.Type() == attribute.STRINGSLICE },
	"app.session.ref":                  regexOf(reRef),
	"app.prompt.version":               enumOf("v1", "v2"),
	"app.order.id":                     regexOf(reUUID),
	"app.order.channel":                enumOf("web", "assistant"),
	"app.order.status":                 enumOf("confirmed", "shipped", "delivered", "cancelled"),
	"app.order.currency":               regexOf(reCurrncy),
	"app.order.item_count":             intIn(0, 10000),
	"app.order.persistence_status":     enumOf("recorded", "history_unrecorded", "pending", "dead"),
	"app.checkout.idempotency_status":  enumOf("new", "replayed", "in_progress", "resumed", "conflict", "rejected"),
	"app.checkout.state":               regexOf(regexp.MustCompile(`^[A-Z_]{3,32}$`)),
	"app.payment.method_type":          enumOf("demo_card_saved", "card_form"),
	"app.shipping.is_po_box":           boolOf,
}

// legacyAppKeys are pre-existing, non-PII business attributes emitted by the frontend before this
// work; they are passed through unchanged.
var legacyAppKeys = map[string]bool{
	"app.request.id": true, "app.currency": true, "app.product.quantity": true, "app.cart.action": true,
	"app.cart.currency": true, "app.cart.item_count": true, "app.cart.total_value": true,
	"app.checkout.failed": true, "app.checkout.failed_cart_currency": true,
	"app.checkout.failed_cart_item_count": true, "app.checkout.failed_cart_value": true,
	"app.product.id": true, "app.product.category": true, "app.product.name": true,
}

// baggageAllowlist is the only set of baggage keys that is accepted from, or sent to, other hops.
var baggageAllowlist = func() map[string]bool {
	m := map[string]bool{"app.customer.id": true, "app.customer.authenticated": true, "app.order.id": true,
		"app.session.ref": true, "app.order.channel": true, "app.prompt.version": true}
	for k := range legacyAppKeys {
		m[k] = true
	}
	return m
}()

// validAppAttr reports whether (key, value) may be exported.
func validAppAttr(kv attribute.KeyValue) bool {
	k := string(kv.Key)
	if legacyAppKeys[k] {
		return true
	}
	if f, ok := appSchema[k]; ok {
		return f(kv.Value)
	}
	return false
}

// setAppAttrs sets typed app.* attributes on the span; invalid or unknown ones are silently dropped.
func setAppAttrs(span trace.Span, kvs ...attribute.KeyValue) {
	for _, kv := range kvs {
		if validAppAttr(kv) {
			span.SetAttributes(kv)
		}
	}
}

// stampAppAttrs copies allowlisted baggage entries onto the current span with their real types.
func stampAppAttrs(ctx context.Context) {
	span := trace.SpanFromContext(ctx)
	if !span.SpanContext().IsValid() {
		return
	}
	for _, m := range baggage.FromContext(ctx).Members() {
		k := m.Key()
		if !strings.HasPrefix(k, "app.") || !baggageAllowlist[k] {
			continue
		}
		if k == "app.customer.authenticated" {
			setAppAttrs(span, attribute.Bool(k, m.Value() == "true"))
			continue
		}
		if n, err := strconv.ParseInt(m.Value(), 10, 64); err == nil && appSchemaIsInt(k) {
			setAppAttrs(span, attribute.Int64(k, n))
			continue
		}
		setAppAttrs(span, attribute.String(k, m.Value()))
	}
}

func appSchemaIsInt(k string) bool {
	f, ok := appSchema[k]
	return ok && f(attribute.Int64Value(1))
}

// ---- fail-closed export sanitizer ---------------------------------------------------------

var (
	reEmailText = regexp.MustCompile(`[A-Za-z0-9._%+'\-]+@[A-Za-z0-9\-]+(?:\.[A-Za-z0-9\-]+)+`)
	rePhoneText = regexp.MustCompile(`(?:^|[^\w.])(?:\+?1[\s.\-]?)?\(?[2-9]\d{2}\)?[\s.\-]?[2-9]\d{2}[\s.\-]?\d{4}(?:[^\w]|$)`)
	reStreetTxt = regexp.MustCompile(`(?i)\b\d{1,6}[A-Za-z]?\s+(?:[A-Za-z0-9.'\-]+\s+){0,4}?(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Drive|Dr|Lane|Ln|Way|Court|Ct|Place|Pl|Parkway|Pkwy|Terrace|Ter|Circle|Cir|Highway|Hwy|Square|Sq|Trail|Trl|Alley|Aly|Mall)\b\.?`)
	rePOBoxTxt  = regexp.MustCompile(`(?i)\b(?:P\.?\s?O\.?\s*Box|Post\s+Office\s+Box)\s*\d+\b`)
	reStateZip  = regexp.MustCompile(`\b[A-Z]{2}\.?,?\s+\d{5}(?:-\d{4})?\b`)
	reCardTxt   = regexp.MustCompile(`\b(?:\d[ -]?){12,18}\d\b`)
	reDemoTok   = regexp.MustCompile(`\btok_demo_[A-Za-z0-9_\-]+`)
	reConnTxt   = regexp.MustCompile(`(?i)\b(?:postgres(?:ql)?|redis)://[^\s"']+`)
	reBearerTxt = regexp.MustCompile(`(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}`)
)

const (
	contactPlaceholder = "<contact_redacted>"
	addressPlaceholder = "<address_redacted>"
	secretPlaceholder  = "<secret_redacted>"
)

func scrubText(s string) string {
	s = reEmailText.ReplaceAllString(s, contactPlaceholder)
	s = reStreetTxt.ReplaceAllString(s, addressPlaceholder)
	s = rePOBoxTxt.ReplaceAllString(s, addressPlaceholder)
	s = reStateZip.ReplaceAllString(s, addressPlaceholder)
	s = rePhoneText.ReplaceAllStringFunc(s, func(m string) string {
		// keep the delimiter characters the lazy regex swallowed
		lead, trail := "", ""
		if len(m) > 0 && !isDigitOrPlus(m[0]) && m[0] != '(' {
			lead = m[:1]
		}
		if n := len(m); n > 0 && !isDigitOrPlus(m[n-1]) {
			trail = m[n-1:]
		}
		return lead + contactPlaceholder + trail
	})
	s = reDemoTok.ReplaceAllString(s, secretPlaceholder)
	s = reConnTxt.ReplaceAllString(s, secretPlaceholder)
	s = reBearerTxt.ReplaceAllString(s, "Bearer "+secretPlaceholder)
	s = reCardTxt.ReplaceAllString(s, "<card>")
	return s
}

func isDigitOrPlus(c byte) bool { return (c >= '0' && c <= '9') || c == '+' }

var droppedAttrPrefixes = []string{"http.request.header", "http.response.header", "url.query", "url.full", "http.url",
	"exception.message", "exception.stacktrace", "db.query.parameter", "rpc.request.metadata", "rpc.response.metadata",
	"http.request.body", "http.response.body"}

func sanitizeAttrs(in []attribute.KeyValue) []attribute.KeyValue {
	out := make([]attribute.KeyValue, 0, len(in))
	for _, kv := range in {
		k := string(kv.Key)
		drop := false
		for _, p := range droppedAttrPrefixes {
			if strings.HasPrefix(k, p) {
				drop = true
				break
			}
		}
		if drop {
			continue
		}
		if strings.HasPrefix(k, "app.") {
			if validAppAttr(kv) {
				out = append(out, kv)
			}
			continue
		}
		switch kv.Value.Type() {
		case attribute.STRING:
			v := kv.Value.AsString()
			if k == "http.target" || k == "url.path" || k == "http.route" {
				v = strings.SplitN(v, "?", 2)[0]
			}
			out = append(out, attribute.String(k, scrubText(v)))
		case attribute.STRINGSLICE:
			vs := kv.Value.AsStringSlice()
			c := make([]string, len(vs))
			for i, v := range vs {
				c[i] = scrubText(v)
			}
			out = append(out, attribute.StringSlice(k, c))
		default:
			out = append(out, kv)
		}
	}
	return out
}

// sanitizeSpans returns sanitized snapshots of the spans (never mutates the originals).
func sanitizeSpans(spans []sdktrace.ReadOnlySpan) []sdktrace.ReadOnlySpan {
	// Negative-control switch: honoured ONLY in test mode, so tests can prove the canary checks fail without it.
	if os.Getenv("TELEMETRY_TEST_MODE") == "1" && os.Getenv("TELEMETRY_SANITIZER_DISABLED") == "1" {
		return spans
	}
	stubs := tracetest.SpanStubsFromReadOnlySpans(spans)
	for i := range stubs {
		s := &stubs[i]
		s.Name = scrubText(s.Name)
		s.Attributes = sanitizeAttrs(s.Attributes)
		for j := range s.Events {
			e := &s.Events[j]
			if e.Name == "exception" {
				kept := []attribute.KeyValue{}
				for _, kv := range e.Attributes {
					if kv.Key == "exception.type" || kv.Key == "exception.escaped" {
						kept = append(kept, kv)
					}
				}
				e.Attributes = kept
			} else {
				e.Attributes = sanitizeAttrs(e.Attributes)
			}
		}
		for j := range s.Links {
			s.Links[j].Attributes = sanitizeAttrs(s.Links[j].Attributes)
		}
		if s.Status.Description != "" {
			d := scrubText(s.Status.Description)
			if len(d) > 64 {
				d = d[:64]
			}
			s.Status.Description = d
		}
	}
	return stubs.Snapshots()
}

// sanitizingExporter is the last gate before OTLP export.
type sanitizingExporter struct{ inner sdktrace.SpanExporter }

func (e sanitizingExporter) ExportSpans(ctx context.Context, spans []sdktrace.ReadOnlySpan) error {
	return e.inner.ExportSpans(ctx, sanitizeSpans(spans))
}
func (e sanitizingExporter) Shutdown(ctx context.Context) error { return e.inner.Shutdown(ctx) }

// allowlistBaggage is W3C baggage restricted to baggageAllowlist on both extract and inject: a
// browser (or any peer) that sends PII in a baggage header never gets it onto our spans or onward.
type allowlistBaggage struct{ inner propagation.Baggage }

func filterBaggage(ctx context.Context) context.Context {
	var kept []baggage.Member
	for _, m := range baggage.FromContext(ctx).Members() {
		if baggageAllowlist[m.Key()] {
			kept = append(kept, m)
		}
	}
	bag, err := baggage.New(kept...)
	if err != nil {
		return baggage.ContextWithoutBaggage(ctx)
	}
	return baggage.ContextWithBaggage(ctx, bag)
}

func (a allowlistBaggage) Extract(ctx context.Context, carrier propagation.TextMapCarrier) context.Context {
	return filterBaggage(a.inner.Extract(ctx, carrier))
}
func (a allowlistBaggage) Inject(ctx context.Context, carrier propagation.TextMapCarrier) {
	a.inner.Inject(filterBaggage(ctx), carrier)
}
func (a allowlistBaggage) Fields() []string { return a.inner.Fields() }
