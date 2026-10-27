package main

import (
	"context"
	"encoding/json"
	"errors"
	"io"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestAccountAPISendsInternalAndSessionHeaders(t *testing.T) {
	var received http.Request
	var receivedBody map[string]interface{}
	server := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		received = *r
		if err := json.NewDecoder(r.Body).Decode(&receivedBody); err != nil {
			t.Fatalf("decode request body: %v", err)
		}
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"session_token":"new-token"}`)
	}))
	defer server.Close()

	previousClient := assistantClient
	assistantClient = server.Client()
	defer func() { assistantClient = previousClient }()

	r := httptest.NewRequest(http.MethodPost, "/account/create", nil)
	r = r.WithContext(context.WithValue(r.Context(), ctxKeySessionID{}, "browser-session"))
	r.AddCookie(&http.Cookie{Name: cookieAuth, Value: "account-token"})
	frontend := &frontendServer{
		shoppingAssistantSvcAddr: strings.TrimPrefix(server.URL, "http://"),
		accountAPISecret:         "shared-secret",
	}

	result, status, err := frontend.accountAPI(r, "/account/register", map[string]interface{}{"email": "demo@example.com"})
	if err != nil {
		t.Fatalf("accountAPI returned error: %v", err)
	}
	if status != http.StatusOK || result["session_token"] != "new-token" {
		t.Fatalf("unexpected accountAPI response: status=%d result=%v", status, result)
	}
	if received.Header.Get("X-Account-Api-Secret") != "shared-secret" {
		t.Fatalf("missing or incorrect internal API secret header")
	}
	if received.Header.Get("X-Session-ID") != "browser-session" {
		t.Fatalf("missing or incorrect session ID header")
	}
	if received.Header.Get("X-Account-Session") != "account-token" {
		t.Fatalf("missing or incorrect account session header")
	}
	if receivedBody["email"] != "demo@example.com" {
		t.Fatalf("unexpected request body: %v", receivedBody)
	}
}

func TestAccountErrorMessage(t *testing.T) {
	tests := []struct {
		name   string
		result map[string]interface{}
		status int
		err    error
		want   string
	}{
		{name: "transport", err: errors.New("connection refused"), want: "The account service is temporarily unavailable. Please try again."},
		{name: "duplicate", status: http.StatusConflict, want: "An account with that email already exists. Try logging in instead."},
		{name: "invalid login", status: http.StatusUnauthorized, want: "The email or password is incorrect."},
		{name: "locked", status: http.StatusTooManyRequests, want: "Too many login attempts. Please try again later."},
		{name: "validation", result: map[string]interface{}{"error": "validation_failed"}, status: http.StatusBadRequest, want: "Please check the account details and try again."},
		{name: "field errors", result: map[string]interface{}{"error": "validation_failed", "fields": map[string]interface{}{
			"zip_code": "Enter a postal code.", "last_name": "Enter your last name.", "first_name": "Enter your first name."}},
			status: http.StatusBadRequest, want: "Enter your first name. Enter your last name. Enter a postal code."},
	}

	for _, test := range tests {
		t.Run(test.name, func(t *testing.T) {
			if got := accountErrorMessage(test.result, test.status, test.err); got != test.want {
				t.Fatalf("accountErrorMessage() = %q, want %q", got, test.want)
			}
		})
	}
}

func activationFrontend(t *testing.T, handler http.HandlerFunc) (*frontendServer, func()) {
	t.Helper()
	server := httptest.NewServer(handler)
	previousClient := assistantClient
	assistantClient = server.Client()
	return &frontendServer{
			shoppingAssistantSvcAddr: strings.TrimPrefix(server.URL, "http://"),
			accountAPISecret:         "shared-secret",
		}, func() {
			assistantClient = previousClient
			server.Close()
		}
}

func sessionRequest(method, target string, body io.Reader) *http.Request {
	r := httptest.NewRequest(method, target, body)
	r = r.WithContext(context.WithValue(r.Context(), ctxKeySessionID{}, "browser-session"))
	r.AddCookie(&http.Cookie{Name: cookieCSRF, Value: "csrf-token"})
	return r
}

func assertNoActivationCookieValue(t *testing.T, w *httptest.ResponseRecorder) {
	t.Helper()
	for _, c := range w.Result().Cookies() {
		if strings.HasPrefix(c.Name, "shop_activation_") && (c.MaxAge >= 0 || c.Value != "") {
			t.Fatalf("activation cookie %q must only ever be cleared, got %+v", c.Name, c)
		}
	}
}

func TestCreateAccountForwardsFirstAndLastName(t *testing.T) {
	var got map[string]interface{}
	fe, done := activationFrontend(t, func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&got)
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusCreated)
		_, _ = io.WriteString(w, `{"session_token":"new-token"}`)
	})
	defer done()
	form := strings.NewReader("csrf=csrf-token&first_name=+Ada+&last_name=Lovelace&email=ada%40example.com&password=Sup3rPassw0rd%21")
	r := sessionRequest(http.MethodPost, "/account/create", form)
	r.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	w := httptest.NewRecorder()
	fe.accountSubmit(w, r, "create")
	if got["first_name"] != "Ada" || got["last_name"] != "Lovelace" {
		t.Fatalf("first/last name not forwarded (trimmed): %v / %v", got["first_name"], got["last_name"])
	}
	if _, ok := got["name"]; ok {
		t.Fatalf("legacy single name field must not be sent")
	}
	if w.Code != http.StatusFound {
		t.Fatalf("status = %d, want 302", w.Code)
	}
}

func TestActivationSubmitSendsNoCodeAndSetsNoActivationCookie(t *testing.T) {
	var got map[string]interface{}
	fe, done := activationFrontend(t, func(w http.ResponseWriter, r *http.Request) {
		_ = json.NewDecoder(r.Body).Decode(&got)
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"session_token":"new-token"}`)
	})
	defer done()

	form := strings.NewReader("csrf=csrf-token&password=Sup3rsecret")
	r := sessionRequest(http.MethodPost, "/account/activation", form)
	r.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	// Stale cookies from earlier builds must not be forwarded.
	r.AddCookie(&http.Cookie{Name: "shop_activation_code", Value: "stale-code"})
	r.AddCookie(&http.Cookie{Name: "shop_activation_email", Value: "stale@example.com"})
	w := httptest.NewRecorder()
	fe.accountSubmit(w, r, "activation")

	if _, ok := got["claim_code"]; ok {
		t.Fatalf("claim_code must not be sent by the frontend: %v", got)
	}
	if _, ok := got["email"]; ok && got["email"] != "" {
		t.Fatalf("email must not come from a cookie: %v", got)
	}
	if got["session_id"] != "browser-session" || got["password"] != "Sup3rsecret" {
		t.Fatalf("unexpected payload: %v", got)
	}
	assertNoActivationCookieValue(t, w)
	if w.Code != http.StatusFound || w.Header().Get("Location") != baseUrl+"/account/profile" {
		t.Fatalf("unexpected redirect: %d %q", w.Code, w.Header().Get("Location"))
	}
}

func TestActivationLoginRequiredRedirectsToLogin(t *testing.T) {
	fe, done := activationFrontend(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"activated":true,"login_required":true}`)
	})
	defer done()
	r := sessionRequest(http.MethodPost, "/account/activation", strings.NewReader("csrf=csrf-token&password=Sup3rsecret"))
	r.Header.Set("Content-Type", "application/x-www-form-urlencoded")
	w := httptest.NewRecorder()
	fe.accountSubmit(w, r, "activation")
	if w.Code != http.StatusFound || !strings.Contains(w.Header().Get("Location"), "/login") {
		t.Fatalf("expected redirect to login, got %d %q", w.Code, w.Header().Get("Location"))
	}
	for _, c := range w.Result().Cookies() {
		if c.Name == cookieAuth {
			t.Fatalf("no auth cookie expected when login_required")
		}
	}
}

func TestActivationPageShowsEmailWithoutCookies(t *testing.T) {
	fe, done := activationFrontend(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		_, _ = io.WriteString(w, `{"pending":true,"email":"demo@example.com"}`)
	})
	defer done()
	w := httptest.NewRecorder()
	fe.renderAccountForm(w, sessionRequest(http.MethodGet, "/account/activation", nil), "Complete Account Setup", "activation")
	assertNoActivationCookieValue(t, w)
	if !strings.Contains(w.Body.String(), "demo@example.com") {
		t.Fatalf("activation page should show the pending account email")
	}
}

func TestActivationPageWithNothingPending(t *testing.T) {
	fe, done := activationFrontend(t, func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusNotFound)
		_, _ = io.WriteString(w, `{"pending":false}`)
	})
	defer done()
	w := httptest.NewRecorder()
	fe.renderAccountForm(w, sessionRequest(http.MethodGet, "/account/activation", nil), "Complete Account Setup", "activation")
	if !strings.Contains(w.Body.String(), "no account setup in progress") {
		t.Fatalf("expected the nothing-pending message, got %q", w.Body.String())
	}
}

func TestProfileKeepsAuthCookieWhenAccountsUnavailable(t *testing.T) {
	fe, done := activationFrontend(t, func(w http.ResponseWriter, r *http.Request) {
		if r.Method != http.MethodGet || r.Header.Get("X-Account-Session") != "auth-token" {
			t.Errorf("profile must be fetched with GET and the session header, got %s %q", r.Method, r.Header.Get("X-Account-Session"))
		}
		w.Header().Set("Content-Type", "application/json")
		w.WriteHeader(http.StatusServiceUnavailable)
		_, _ = io.WriteString(w, `{"error":"account_store_unavailable"}`)
	})
	defer done()
	r := sessionRequest(http.MethodGet, "/account/profile", nil)
	r.AddCookie(&http.Cookie{Name: cookieAuth, Value: "auth-token"})
	w := httptest.NewRecorder()
	fe.accountProfileHandler(w, r)
	if w.Code == http.StatusFound {
		t.Fatalf("must not redirect a logged-in customer to login during an outage")
	}
	for _, c := range w.Result().Cookies() {
		if c.Name == cookieAuth {
			t.Fatalf("auth cookie must not be modified during an outage: %+v", c)
		}
	}
	if !strings.Contains(w.Body.String(), "temporarily unavailable") {
		t.Fatalf("expected unavailable message")
	}
}
