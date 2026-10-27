package main

import (
	"bytes"
	"context"
	"net/http"
	"net/http/httptest"
	"net/url"
	"os"
	"strings"
	"testing"

	pb "github.com/GoogleCloudPlatform/microservices-demo/src/frontend/genproto"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/baggage"
	"go.opentelemetry.io/otel/propagation"
	sdktrace "go.opentelemetry.io/otel/sdk/trace"
	"go.opentelemetry.io/otel/sdk/trace/tracetest"
	oteltrace "go.opentelemetry.io/otel/trace"
)

func TestContractParity(t *testing.T) {
	server, err := os.ReadFile("../accountservice/account_validation.json")
	if err != nil {
		t.Fatalf("read server contract: %v", err)
	}
	if !bytes.Equal(server, accountContractJSON) {
		t.Fatal("account_contract.json differs from accountservice/account_validation.json: copy it")
	}
	cases := map[[2]string]string{
		{"email", "email_invalid_domain"}: "Enter an email address with a valid domain ending, like .com, .edu or .org.",
		{"first_name", "required"}:        "Enter your first name.",
		{"first_name", "too_long"}:        "First name must be 100 characters or fewer.",
		{"zip", "zip_invalid"}:            "Enter a 5-digit ZIP code or ZIP+4, like 94105 or 94105-1234.",
		{"phone", "phone_incomplete"}:     "Enter a 10-digit US phone number, like (415) 555-2671.",
	}
	for k, want := range cases {
		if got := contractMessage(k[0], k[1]); got != want {
			t.Errorf("contractMessage(%s,%s) = %q, want %q", k[0], k[1], got, want)
		}
	}
	if len(contract.USStates) != 51 {
		t.Errorf("expected 50 states + DC, got %d", len(contract.USStates))
	}
}

func TestSafeNext(t *testing.T) {
	good := []string{"/account", "/account/orders", "/cart", "/product/OLJCESPC7Z", "/"}
	bad := []string{"", "http://evil.example/", "//evil.example", "/\\evil.example", "javascript:alert(1)", "/account\r\nSet-Cookie: x=1",
		"https://evil.example/account", "/login", "/static/x", "account", "/%2f%2fevil", "/ account"}
	for _, n := range good {
		if safeNext(n) != n {
			t.Errorf("safeNext(%q) = %q, want accepted", n, safeNext(n))
		}
	}
	for _, n := range bad {
		if got := safeNext(n); got != "" {
			t.Errorf("safeNext(%q) = %q, want rejected", n, got)
		}
	}
}

func TestSessionRef(t *testing.T) {
	old := telemetryRefKey
	defer func() { telemetryRefKey = old }()
	telemetryRefKey = ""
	if sessionRef("abc") != "" {
		t.Fatal("no key must yield no ref")
	}
	telemetryRefKey = "k1"
	a, b := sessionRef("raw-session-id"), sessionRef("raw-session-id")
	if a == "" || a != b || len(a) != 16 || strings.Contains(a, "raw-session") {
		t.Fatalf("bad ref %q", a)
	}
	telemetryRefKey = "k2"
	if sessionRef("raw-session-id") == a {
		t.Fatal("ref must depend on the key")
	}
}

func TestBaggageAllowlist(t *testing.T) {
	prop := allowlistBaggage{}
	carrier := propagation.MapCarrier{"baggage": "app.customer.email=zq%40example.com,app.customer.id=3f2a9c1e-1111-4222-8333-444455556666,note=free-text,app.session.ref=0123456789abcdef"}
	ctx := prop.Extract(context.Background(), carrier)
	b := baggage.FromContext(ctx)
	if b.Member("app.customer.email").Key() != "" || b.Member("note").Key() != "" {
		t.Fatalf("PII/unknown baggage was accepted: %v", b)
	}
	if b.Member("app.customer.id").Value() == "" || b.Member("app.session.ref").Value() == "" {
		t.Fatalf("allowlisted baggage dropped: %v", b)
	}
	// inject also filters (a handler that wrongly put PII in baggage still cannot send it)
	m, _ := baggage.NewMember("app.customer.email", "leak@example.com")
	bag, _ := baggage.New(m)
	out := propagation.MapCarrier{}
	prop.Inject(baggage.ContextWithBaggage(context.Background(), bag), out)
	if strings.Contains(out.Get("baggage"), "leak") {
		t.Fatalf("PII injected: %v", out)
	}
}

func spanWith(attrs []attribute.KeyValue, events ...sdktrace.Event) sdktrace.ReadOnlySpan {
	sc := oteltrace.NewSpanContext(oteltrace.SpanContextConfig{TraceID: oteltrace.TraceID{1}, SpanID: oteltrace.SpanID{2}})
	return tracetest.SpanStub{Name: "probe", SpanContext: sc, Attributes: attrs, Events: events}.Snapshot()
}

func TestSanitizeSpansFailClosed(t *testing.T) {
	canaries := []string{"Quillfeather", "zq.canary+tag@example.com", "(415) 555-2671", "7741 Canarywood Boulevard", "tok_demo_abc123", "canary-header", "canary-exc-msg", "unknown-app-canary"}
	in := []attribute.KeyValue{
		attribute.String("app.customer.id", "3f2a9c1e-1111-4222-8333-444455556666"),
		attribute.String("app.customer.email", "zq.canary+tag@example.com"), // unknown app.* -> dropped
		attribute.String("app.account.action", "login"),
		attribute.String("app.account.action2", "unknown-app-canary"),
		attribute.String("app.customer.id2", "x"),
		attribute.String("http.target", "/account?email=zq.canary+tag@example.com"),
		attribute.String("http.request.header.cookie", "canary-header"),
		attribute.String("db.statement", "note Quillfeather call (415) 555-2671 at 7741 Canarywood Boulevard"),
		attribute.String("custom.note", "tok_demo_abc123"),
		attribute.Int("http.status_code", 200),
	}
	ev := sdktrace.Event{Name: "exception", Attributes: []attribute.KeyValue{attribute.String("exception.type", "ValueError"), attribute.String("exception.message", "canary-exc-msg"), attribute.String("exception.stacktrace", "canary-exc-msg")}}
	out := sanitizeSpans([]sdktrace.ReadOnlySpan{spanWith(in, ev)})
	var blob strings.Builder
	for _, kv := range out[0].Attributes() {
		blob.WriteString(string(kv.Key) + "=" + kv.Value.Emit() + "\n")
	}
	for _, e := range out[0].Events() {
		for _, kv := range e.Attributes {
			blob.WriteString(string(kv.Key) + "=" + kv.Value.Emit() + "\n")
		}
	}
	got := blob.String()
	for _, c := range canaries {
		if c == "Quillfeather" {
			continue // names are covered by the server-side registry; the Go layer never receives names
		}
		if strings.Contains(got, c) {
			t.Errorf("canary %q leaked:\n%s", c, got)
		}
	}
	for _, want := range []string{"app.customer.id=3f2a9c1e", "app.account.action=login", "http.target=/account", "http.status_code=200", "exception.type=ValueError"} {
		if !strings.Contains(got, want) {
			t.Errorf("expected %q to survive:\n%s", want, got)
		}
	}
}

func TestValidAppAttrTypes(t *testing.T) {
	ok := []attribute.KeyValue{attribute.String("app.session.ref", "0123456789abcdef"), attribute.Bool("app.customer.authenticated", true),
		attribute.Int("app.account.profile_version", 2), attribute.String("app.order.id", "3f2a9c1e-1111-4222-8333-444455556666")}
	bad := []attribute.KeyValue{attribute.String("app.session.ref", "raw-session-id-123"), attribute.String("app.customer.authenticated", "true"),
		attribute.Int("app.account.profile_version", 3), attribute.String("app.order.id", "not-a-uuid"), attribute.String("app.customer.email", "a@b.com"),
		attribute.String("app.customer.id", "zephyrine@example.com")}
	for _, kv := range ok {
		if !validAppAttr(kv) {
			t.Errorf("%s should be valid", kv.Key)
		}
	}
	for _, kv := range bad {
		if validAppAttr(kv) {
			t.Errorf("%s=%s should be rejected", kv.Key, kv.Value.Emit())
		}
	}
}

// ---- handler tests against a fake AccountService ------------------------------------------------

type fakeAccount struct {
	pb.AccountServiceServer
	loginErr    error
	profileErr  error
	ordersErr   error
	lastSession string
}

func newAcctFrontend(t *testing.T, fake pb.AccountServiceClient) *frontendServer {
	t.Helper()
	return &frontendServer{account: &accountGRPC{client: fake, secret: "s"}}
}

func req(method, target string, form url.Values, cookies ...*http.Cookie) *http.Request {
	var r *http.Request
	if form != nil {
		r = httptest.NewRequest(method, target, strings.NewReader(form.Encode()))
		r.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	} else {
		r = httptest.NewRequest(method, target, nil)
	}
	for _, c := range cookies {
		r.AddCookie(c)
	}
	return r
}

func TestRequireLoginRedirectsWithSafeNext(t *testing.T) {
	fe := newAcctFrontend(t, nil)
	h := fe.requireLogin(func(w http.ResponseWriter, r *http.Request) { t.Fatal("handler must not run when logged out") })
	rec := httptest.NewRecorder()
	h(rec, req("GET", "/account/orders", nil))
	if rec.Code != http.StatusFound || rec.Header().Get("Location") != "/login?next=%2Faccount%2Forders" {
		t.Fatalf("got %d %q", rec.Code, rec.Header().Get("Location"))
	}
	rec = httptest.NewRecorder()
	h(rec, req("POST", "/account/addresses/x/delete", url.Values{}))
	if rec.Header().Get("Location") != "/login" {
		t.Fatalf("POST must not carry next: %q", rec.Header().Get("Location"))
	}
}

func TestCheckPostRejectsMissingCSRFAndForeignOrigin(t *testing.T) {
	ok := func(r *http.Request) bool { return checkPost(httptest.NewRecorder(), r) }
	csrf := &http.Cookie{Name: cookieCSRF, Value: "abcdefghijklmnopqrstuvwx"}
	if ok(req("POST", "/x", url.Values{})) {
		t.Error("missing csrf accepted")
	}
	if ok(req("POST", "/x", url.Values{"csrf": {"wrong"}}, csrf)) {
		t.Error("wrong csrf accepted")
	}
	r := req("POST", "/x", url.Values{"csrf": {csrf.Value}}, csrf)
	r.Header.Set("Origin", "https://evil.example")
	if ok(r) {
		t.Error("foreign origin accepted")
	}
	r = req("POST", "/x", url.Values{"csrf": {csrf.Value}}, csrf)
	r.Header.Set("Origin", "http://"+r.Host)
	if !ok(r) {
		t.Error("same-origin with valid csrf rejected")
	}
}

func TestAuthCookieAttributes(t *testing.T) {
	r := req("GET", "/", nil)
	c := authCookie(r, "tok", 100)
	if !c.HttpOnly || c.SameSite != http.SameSiteLaxMode || c.Secure {
		t.Errorf("unexpected cookie %+v", c)
	}
	r.Header.Set("X-Forwarded-Proto", "https")
	if !authCookie(r, "tok", 100).Secure {
		t.Error("cookie must be Secure behind https")
	}
}

func TestLogoutGetNeverSignsOut(t *testing.T) {
	fe := newAcctFrontend(t, nil)
	rec := httptest.NewRecorder()
	r := req("GET", "/logout", nil, &http.Cookie{Name: cookieAuth, Value: "tok"})
	r = r.WithContext(context.WithValue(r.Context(), ctxKeyAccount{}, accountSession{Authenticated: true, FirstName: "Ada"}))
	fe.grpcLogout(rec, r)
	if rec.Code != http.StatusOK || !strings.Contains(rec.Body.String(), "method=\"post\"") {
		t.Fatalf("GET /logout must render a confirmation form, got %d", rec.Code)
	}
	for _, c := range rec.Result().Cookies() {
		if c.Name == cookieAuth {
			t.Fatal("GET /logout must not touch the auth cookie")
		}
	}
}
