package main

import (
	"bytes"
	"context"
	cryptorand "crypto/rand"
	"crypto/subtle"
	"encoding/base64"
	"net/http"
	"net/url"
	"regexp"
	"strings"
	"sync"
	"time"

	pb "github.com/GoogleCloudPlatform/microservices-demo/src/frontend/genproto"
	"github.com/GoogleCloudPlatform/microservices-demo/src/frontend/validator"
	"github.com/gorilla/mux"
	"github.com/pkg/errors"
	"github.com/sirupsen/logrus"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"
)

// This file implements the customer account experience backed by accountservice
// (ACCOUNT_BACKEND=grpc): login redirect, dashboard, profile editing, saved addresses and the
// orders / payment-method panels. With ACCOUNT_BACKEND=local the pre-existing flows are used.

type ctxKeyAccount struct{}

type accountSession struct {
	Authenticated   bool
	Unknown         bool // accountservice unreachable; the cookie is kept, identity is not known
	CustomerID      string
	FirstName       string
	ProfileComplete bool
}

var acctTracer = otel.Tracer("frontend.account")

func accountFromContext(ctx context.Context) accountSession {
	if v, ok := ctx.Value(ctxKeyAccount{}).(accountSession); ok {
		return v
	}
	return accountSession{}
}

// accountContext resolves the account session once per request (Redis-only on the server, so it
// works during a Postgres outage) and seeds allowlisted identity baggage for downstream hops.
func (fe *frontendServer) accountContext(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		if fe.account == nil {
			next.ServeHTTP(w, r)
			return
		}
		sess := accountSession{}
		if cookieValue(r, cookieAuth) != "" {
			ctx, cancel := fe.account.ctx(r)
			resp, err := fe.account.client.ResolveSession(ctx, &pb.Empty{})
			cancel()
			switch {
			case err != nil:
				sess = accountSession{Authenticated: true, Unknown: true}
			case resp.GetAuthenticated():
				sess = accountSession{Authenticated: true, CustomerID: resp.GetCustomerId(), FirstName: resp.GetFirstName(), ProfileComplete: resp.GetProfileComplete()}
			}
		}
		ctx := context.WithValue(r.Context(), ctxKeyAccount{}, sess)
		ctx = withBaggage(ctx, "app.customer.authenticated", boolStr(sess.Authenticated && !sess.Unknown))
		if sess.CustomerID != "" {
			ctx = withBaggage(ctx, "app.customer.id", sess.CustomerID)
		}
		stampAppAttrs(ctx)
		next.ServeHTTP(w, r.WithContext(ctx))
	})
}

func boolStr(b bool) string {
	if b {
		return "true"
	}
	return "false"
}

// ---- helpers ---------------------------------------------------------------------------------

func secureRequest(r *http.Request) bool {
	return r.TLS != nil || strings.EqualFold(r.Header.Get("X-Forwarded-Proto"), "https")
}

func authCookie(r *http.Request, token string, maxAge int) *http.Cookie {
	return &http.Cookie{Name: cookieAuth, Value: token, Path: "/", MaxAge: maxAge, HttpOnly: true,
		SameSite: http.SameSiteLaxMode, Secure: secureRequest(r)}
}

// ensureCSRF returns the double-submit token, creating the cookie when missing.
func ensureCSRF(w http.ResponseWriter, r *http.Request) string {
	if c, _ := r.Cookie(cookieCSRF); c != nil && len(c.Value) >= 16 {
		return c.Value
	}
	b := make([]byte, 24)
	_, _ = cryptorand.Read(b)
	v := base64.RawURLEncoding.EncodeToString(b)
	http.SetCookie(w, &http.Cookie{Name: cookieCSRF, Value: v, Path: "/", MaxAge: cookieMaxAge, HttpOnly: true,
		SameSite: http.SameSiteLaxMode, Secure: secureRequest(r)})
	return v
}

// checkPost enforces same-origin + double-submit CSRF on state-changing requests.
func checkPost(w http.ResponseWriter, r *http.Request) bool {
	if o := r.Header.Get("Origin"); o != "" {
		if u, err := url.Parse(o); err != nil || !strings.EqualFold(u.Host, r.Host) {
			http.Error(w, "invalid request", http.StatusForbidden)
			return false
		}
	}
	c, _ := r.Cookie(cookieCSRF)
	form := r.FormValue("csrf")
	if c == nil || c.Value == "" || subtle.ConstantTimeCompare([]byte(c.Value), []byte(form)) != 1 {
		http.Error(w, "invalid request", http.StatusForbidden)
		return false
	}
	return true
}

// safeNext only accepts same-site absolute paths, never an external or protocol-relative URL.
func safeNext(next string) string {
	if next == "" || next[0] != '/' || strings.HasPrefix(next, "//") || strings.ContainsAny(next, "\\\r\n\t ") {
		return ""
	}
	u, err := url.Parse(next)
	if err != nil || u.Scheme != "" || u.Host != "" || u.Opaque != "" {
		return ""
	}
	path := strings.TrimPrefix(u.Path, baseUrl)
	if path == "" {
		path = "/"
	}
	for _, p := range []string{"/account", "/cart", "/product/", "/assistant", "/orders"} {
		if path == p || strings.HasPrefix(path, p+"/") || strings.HasPrefix(path, p) && strings.HasSuffix(p, "/") {
			return baseUrl + path
		}
	}
	if path == "/" {
		return baseUrl + "/"
	}
	return ""
}

func acctSpan(r *http.Request, action string) {
	span := trace.SpanFromContext(r.Context())
	kvs := []attribute.KeyValue{attribute.String("app.account.action", action)}
	if s := accountFromContext(r.Context()); s.Authenticated && !s.Unknown {
		kvs = append(kvs, attribute.String("app.customer.type", "authenticated"), attribute.Bool("app.customer.profile_complete", s.ProfileComplete))
	} else if !s.Authenticated {
		kvs = append(kvs, attribute.String("app.customer.type", "anonymous"))
	}
	setAppAttrs(span, kvs...)
	if ref := sessionRef(sessionID(r)); ref != "" {
		setAppAttrs(span, attribute.String("app.session.ref", ref))
	}
}

func acctOutcome(r *http.Request, outcome string, aerr *accountErr) {
	span := trace.SpanFromContext(r.Context())
	setAppAttrs(span, attribute.String("app.account.outcome", outcome))
	if aerr != nil && len(aerr.Fields) > 0 {
		var fields, codes []string
		for f, e := range aerr.Fields {
			fields = append(fields, f)
			codes = append(codes, e.Code)
		}
		setAppAttrs(span, attribute.StringSlice("app.validation.fields", fields), attribute.StringSlice("app.validation.codes", codes))
	}
}

func (fe *frontendServer) renderAcct(w http.ResponseWriter, r *http.Request, name string, status int, data map[string]interface{}) {
	if data == nil {
		data = map[string]interface{}{}
	}
	if data["values"] == nil {
		data["values"] = map[string]string{}
	}
	if data["field_errors"] == nil {
		data["field_errors"] = map[string]string{}
	}
	data["csrf"] = ensureCSRF(w, r)
	data["states"] = contract.USStates
	data["currencies_list"] = contract.SupportedCurrencies
	var buf bytes.Buffer
	if err := templates.ExecuteTemplate(&buf, name, injectCommonTemplateData(r, data)); err != nil {
		if l, ok := r.Context().Value(ctxKeyLog{}).(logrus.FieldLogger); ok {
			l.WithField("template", name).Errorf("account template failed: %v", err)
		}
		http.Error(w, "internal error", http.StatusInternalServerError)
		return
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.Header().Set("Cache-Control", "no-store")
	if status != 0 && status != http.StatusOK {
		w.WriteHeader(status)
	}
	_, _ = w.Write(buf.Bytes())
}

func (fe *frontendServer) requireLogin(h http.HandlerFunc) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		s := accountFromContext(r.Context())
		if !s.Authenticated {
			if cookieValue(r, cookieAuth) != "" {
				http.SetCookie(w, authCookie(r, "", -1)) // stale/expired session
			}
			target := baseUrl + "/login"
			if n := safeNext(r.URL.Path); n != "" && r.Method == http.MethodGet {
				target += "?next=" + url.QueryEscape(n)
			}
			http.Redirect(w, r, target, http.StatusFound)
			return
		}
		h(w, r)
	}
}

func unavailablePage(w http.ResponseWriter, r *http.Request, fe *frontendServer, what string) {
	fe.renderAcct(w, r, "acct_unavailable", http.StatusServiceUnavailable, map[string]interface{}{"title": "Temporarily unavailable", "what": what})
}

func formAddress(r *http.Request) *pb.AccountAddress {
	return &pb.AccountAddress{
		Line1: strings.TrimSpace(r.FormValue("address_line1")), Line2: strings.TrimSpace(r.FormValue("address_line2")),
		City: strings.TrimSpace(r.FormValue("city")), State: strings.TrimSpace(r.FormValue("state")),
		Zip: strings.TrimSpace(r.FormValue("zip")), Country: strings.TrimSpace(r.FormValue("country")),
	}
}

func formValues(r *http.Request, keys ...string) map[string]string {
	out := map[string]string{}
	for _, k := range keys {
		out[k] = r.FormValue(k)
	}
	return out
}

func fieldMessages(e *accountErr) map[string]string {
	out := map[string]string{}
	if e == nil {
		return out
	}
	for f, fe := range e.Fields {
		m := fe.Message
		if m == "" {
			m = contractMessage(f, fe.Code)
		}
		out[f] = m
	}
	return out
}

// ---- login / signup / activation / logout -----------------------------------------------------

func (fe *frontendServer) grpcLogin(w http.ResponseWriter, r *http.Request) {
	next := safeNext(r.FormValue("next"))
	if r.Method == http.MethodGet {
		acctSpan(r, "login")
		if s := accountFromContext(r.Context()); s.Authenticated && !s.Unknown {
			http.Redirect(w, r, baseUrl+"/account", http.StatusFound)
			return
		}
		notice := ""
		if r.URL.Query().Get("notice") == "account_ready" {
			notice = "Your account is ready - please log in."
		}
		fe.renderAcct(w, r, "acct_login", 0, map[string]interface{}{"title": "Log In", "next": next, "notice": notice})
		return
	}
	if !checkPost(w, r) {
		return
	}
	acctSpan(r, "login")
	ctx, cancel := fe.account.ctx(r)
	defer cancel()
	resp, err := fe.account.client.Login(ctx, &pb.LoginRequest{Email: strings.TrimSpace(r.FormValue("email")), Password: r.FormValue("password")})
	if err != nil {
		aerr := decodeAccountErr(err)
		acctOutcome(r, "failed", aerr)
		msg := "The email or password is incorrect."
		status := http.StatusUnauthorized
		switch {
		case aerr.unavailable():
			msg, status = "Sign-in is temporarily unavailable. Please try again shortly.", http.StatusServiceUnavailable
		case aerr.Code == codes.ResourceExhausted:
			msg, status = "Too many login attempts. Please try again later.", http.StatusTooManyRequests
		}
		fe.renderAcct(w, r, "acct_login", status, map[string]interface{}{"title": "Log In", "next": next, "error": msg, "values": formValues(r, "email")})
		return
	}
	acctOutcome(r, "success", nil)
	fe.claimGuestOrders(r, resp.GetSessionToken())
	http.SetCookie(w, authCookie(r, resp.GetSessionToken(), cookieMaxAge))
	dest := baseUrl + "/account"
	if next != "" {
		dest = next
	}
	http.Redirect(w, r, dest, http.StatusFound)
}

func (fe *frontendServer) grpcSignup(w http.ResponseWriter, r *http.Request) {
	acctSpan(r, "register")
	if r.Method == http.MethodGet {
		fe.renderAcct(w, r, "acct_signup", 0, map[string]interface{}{"title": "Create Account", "values": map[string]string{"preferred_currency": "USD"}})
		return
	}
	if !checkPost(w, r) {
		return
	}
	ctx, cancel := fe.account.ctx(r)
	defer cancel()
	resp, err := fe.account.client.Register(ctx, &pb.RegisterRequest{Input: &pb.RegistrationInput{
		FirstName: strings.TrimSpace(r.FormValue("first_name")), LastName: strings.TrimSpace(r.FormValue("last_name")),
		Email: strings.TrimSpace(r.FormValue("email")), Phone: strings.TrimSpace(r.FormValue("phone")),
		Address: formAddress(r), Password: r.FormValue("password"), ConfirmPassword: r.FormValue("confirm_password"),
		PreferredCurrency: r.FormValue("preferred_currency"),
	}})
	if err != nil {
		aerr := decodeAccountErr(err)
		acctOutcome(r, "rejected", aerr)
		data := map[string]interface{}{"title": "Create Account", "values": formValues(r, "first_name", "last_name", "email", "phone", "address_line1", "address_line2", "city", "state", "zip", "preferred_currency"),
			"field_errors": fieldMessages(aerr)}
		status := http.StatusUnprocessableEntity
		if aerr.unavailable() {
			data["error"], status = "Account creation is temporarily unavailable. Please try again shortly.", http.StatusServiceUnavailable
		} else if len(aerr.Fields) == 0 {
			data["error"] = "The account could not be created. Please try again."
		}
		fe.renderAcct(w, r, "acct_signup", status, data)
		return
	}
	acctOutcome(r, "created", nil)
	fe.claimGuestOrders(r, resp.GetSessionToken())
	http.SetCookie(w, authCookie(r, resp.GetSessionToken(), cookieMaxAge))
	http.Redirect(w, r, baseUrl+"/account", http.StatusFound)
}

func (fe *frontendServer) grpcActivation(w http.ResponseWriter, r *http.Request) {
	acctSpan(r, "activate")
	clearActivationCookies(w)
	ctx, cancel := fe.account.ctx(r)
	defer cancel()
	if r.Method == http.MethodGet {
		info, err := fe.account.client.GetActivation(ctx, &pb.GetActivationRequest{SessionId: sessionID(r)})
		if err != nil {
			fe.renderAcct(w, r, "acct_activation", http.StatusServiceUnavailable, map[string]interface{}{"title": "Complete Account Setup", "error": "Account setup is temporarily unavailable. Please try again."})
			return
		}
		if !info.GetPending() {
			fe.renderAcct(w, r, "acct_activation", http.StatusNotFound, map[string]interface{}{"title": "Complete Account Setup", "error": "There is no account setup in progress for this browser."})
			return
		}
		fe.renderAcct(w, r, "acct_activation", 0, map[string]interface{}{"title": "Complete Account Setup", "email": info.GetEmailDisplay(), "pending": true})
		return
	}
	if !checkPost(w, r) {
		return
	}
	resp, err := fe.account.client.Claim(ctx, &pb.ClaimRequest{SessionId: sessionID(r), Password: r.FormValue("password")})
	if err != nil {
		aerr := decodeAccountErr(err)
		acctOutcome(r, "failed", aerr)
		data := map[string]interface{}{"title": "Complete Account Setup", "pending": true, "field_errors": fieldMessages(aerr)}
		status := http.StatusUnprocessableEntity
		if aerr.Code == codes.NotFound {
			data["error"], data["pending"], status = "There is no account setup in progress for this browser.", false, http.StatusNotFound
		} else if aerr.unavailable() {
			data["error"], status = "Account setup is temporarily unavailable. Please try again.", http.StatusServiceUnavailable
		}
		fe.renderAcct(w, r, "acct_activation", status, data)
		return
	}
	acctOutcome(r, "activated", nil)
	if resp.GetSessionToken() == "" {
		http.Redirect(w, r, baseUrl+"/login?notice=account_ready", http.StatusFound)
		return
	}
	fe.claimGuestOrders(r, resp.GetSessionToken())
	http.SetCookie(w, authCookie(r, resp.GetSessionToken(), cookieMaxAge))
	http.Redirect(w, r, baseUrl+"/account", http.StatusFound)
}

// grpcLogout: GET shows a confirmation (so a link or image tag can't sign anyone out); POST + CSRF signs out.
func (fe *frontendServer) grpcLogout(w http.ResponseWriter, r *http.Request) {
	acctSpan(r, "logout")
	if r.Method == http.MethodGet {
		if !accountFromContext(r.Context()).Authenticated {
			http.Redirect(w, r, baseUrl+"/", http.StatusFound)
			return
		}
		fe.renderAcct(w, r, "acct_logout", 0, map[string]interface{}{"title": "Sign out"})
		return
	}
	if !checkPost(w, r) {
		return
	}
	if cookieValue(r, cookieAuth) != "" {
		ctx, cancel := fe.account.ctx(r)
		_, _ = fe.account.client.Logout(ctx, &pb.Empty{})
		cancel()
		http.SetCookie(w, authCookie(r, "", -1))
	}
	acctOutcome(r, "success", nil)
	http.Redirect(w, r, baseUrl+"/", http.StatusFound)
}

// ---- dashboard ----------------------------------------------------------------------------------

type dashPanels struct {
	Profile       *pb.Profile
	ProfileErr    *accountErr
	Addresses     []*pb.SavedAddress
	AddressesErr  *accountErr
	Orders        []*pb.OrderSummary
	OrdersPending bool
	OrdersErr     *accountErr
	Payments      []*pb.PaymentMethod
	PaymentsErr   *accountErr
}

func (fe *frontendServer) accountDashboard(w http.ResponseWriter, r *http.Request) {
	acctSpan(r, "dashboard_view")
	ctx, span := acctTracer.Start(r.Context(), "account.dashboard")
	r = r.WithContext(ctx)
	defer span.End()
	var p dashPanels
	var wg sync.WaitGroup
	call := func(f func(context.Context)) {
		wg.Add(1)
		go func() {
			defer wg.Done()
			c, cancel := fe.account.ctx(r)
			defer cancel()
			f(c)
		}()
	}
	call(func(c context.Context) {
		v, err := fe.account.client.GetProfile(c, &pb.Empty{})
		p.Profile, p.ProfileErr = v, decodeAccountErr(err)
	})
	call(func(c context.Context) {
		v, err := fe.account.client.ListAddresses(c, &pb.Empty{})
		p.Addresses, p.AddressesErr = v.GetAddresses(), decodeAccountErr(err)
	})
	call(func(c context.Context) {
		v, err := fe.account.client.ListOrders(c, &pb.ListOrdersRequest{PageSize: 3})
		p.Orders, p.OrdersPending, p.OrdersErr = v.GetOrders(), v.GetQueueDepth() > 0, decodeAccountErr(err)
	})
	call(func(c context.Context) {
		v, err := fe.account.client.ListPaymentMethods(c, &pb.Empty{})
		p.Payments, p.PaymentsErr = v.GetMethods(), decodeAccountErr(err)
	})
	wg.Wait()
	for _, e := range []*accountErr{p.ProfileErr, p.AddressesErr, p.OrdersErr, p.PaymentsErr} {
		if e != nil && e.Code == codes.Unauthenticated {
			http.SetCookie(w, authCookie(r, "", -1))
			http.Redirect(w, r, baseUrl+"/login?next="+url.QueryEscape(baseUrl+"/account"), http.StatusFound)
			return
		}
	}
	failures := 0
	for _, e := range []*accountErr{p.ProfileErr, p.AddressesErr, p.OrdersErr, p.PaymentsErr} {
		if e != nil {
			failures++
		}
	}
	setAppAttrs(span, attribute.Int("app.account.panel_failures", failures),
		attribute.Int("app.account.address_count", len(p.Addresses)), attribute.Int("app.account.payment_method_count", len(p.Payments)))
	setAppAttrs(trace.SpanFromContext(r.Context()), attribute.Int("app.account.panel_failures", failures))
	var defaultAddr *pb.SavedAddress
	for _, a := range p.Addresses {
		if a.GetIsDefault() {
			defaultAddr = a
		}
	}
	fe.renderAcct(w, r, "acct_dashboard", 0, map[string]interface{}{"title": "My Account", "p": p, "default_address": defaultAddr, "nav": "dashboard"})
}

// ---- profile ----------------------------------------------------------------------------------

var profileFields = []string{"first_name", "last_name", "phone", "preferred_currency"}

func (fe *frontendServer) accountProfile(w http.ResponseWriter, r *http.Request) {
	acctSpan(r, "profile_update")
	ctx, cancel := fe.account.ctx(r)
	defer cancel()
	if r.Method == http.MethodGet {
		acctSpan(r, "profile_get")
		prof, err := fe.account.client.GetProfile(ctx, &pb.Empty{})
		if err != nil {
			fe.accountFailure(w, r, decodeAccountErr(err), "your profile")
			return
		}
		fe.renderAcct(w, r, "acct_profile", 0, map[string]interface{}{"title": "Profile", "nav": "profile", "profile": prof,
			"values": map[string]string{"first_name": prof.GetFirstName(), "last_name": prof.GetLastName(), "phone": prof.GetPhoneDisplay(), "preferred_currency": prof.GetPreferredCurrency()}})
		return
	}
	if !checkPost(w, r) {
		return
	}
	req := &pb.UpdateProfileRequest{FirstName: strings.TrimSpace(r.FormValue("first_name")), LastName: strings.TrimSpace(r.FormValue("last_name")),
		Phone: strings.TrimSpace(r.FormValue("phone")), PreferredCurrency: r.FormValue("preferred_currency"), Fields: profileFields}
	prof, err := fe.account.client.UpdateProfile(ctx, req)
	if err != nil {
		aerr := decodeAccountErr(err)
		if len(aerr.Fields) == 0 {
			fe.accountFailure(w, r, aerr, "your profile")
			return
		}
		acctOutcome(r, "rejected", aerr)
		cur, _ := fe.account.client.GetProfile(ctx, &pb.Empty{})
		fe.renderAcct(w, r, "acct_profile", http.StatusUnprocessableEntity, map[string]interface{}{"title": "Profile", "nav": "profile", "profile": cur,
			"values": formValues(r, profileFields...), "field_errors": fieldMessages(aerr)})
		return
	}
	acctOutcome(r, "updated", nil)
	setAppAttrs(trace.SpanFromContext(r.Context()), attribute.Int("app.account.profile_version", int(prof.GetProfileVersion())))
	http.Redirect(w, r, baseUrl+"/account?updated=profile", http.StatusFound)
}

// accountFailure maps a backend failure to a page: login redirect, 503, or a generic error.
func (fe *frontendServer) accountFailure(w http.ResponseWriter, r *http.Request, aerr *accountErr, what string) {
	switch {
	case aerr.Code == codes.Unauthenticated:
		http.SetCookie(w, authCookie(r, "", -1))
		http.Redirect(w, r, baseUrl+"/login", http.StatusFound)
	case aerr.unavailable():
		acctOutcome(r, "unavailable", nil)
		unavailablePage(w, r, fe, what)
	case aerr.Code == codes.NotFound:
		fe.renderAcct(w, r, "acct_unavailable", http.StatusNotFound, map[string]interface{}{"title": "Not found", "what": what, "not_found": true})
	default:
		acctOutcome(r, "error", nil)
		fe.renderAcct(w, r, "acct_unavailable", http.StatusInternalServerError, map[string]interface{}{"title": "Something went wrong", "what": what})
	}
}

// ---- addresses --------------------------------------------------------------------------------

func (fe *frontendServer) accountAddresses(w http.ResponseWriter, r *http.Request) {
	acctSpan(r, "address_list")
	ctx, cancel := fe.account.ctx(r)
	defer cancel()
	list, err := fe.account.client.ListAddresses(ctx, &pb.Empty{})
	if err != nil {
		fe.accountFailure(w, r, decodeAccountErr(err), "your saved addresses")
		return
	}
	fe.renderAcct(w, r, "acct_addresses", 0, map[string]interface{}{"title": "Saved addresses", "nav": "addresses", "addresses": list.GetAddresses(), "notice": r.URL.Query().Get("notice")})
}

func (fe *frontendServer) accountAddressForm(w http.ResponseWriter, r *http.Request) {
	id := mux.Vars(r)["id"]
	action := "address_create"
	if id != "" {
		action = "address_update"
	}
	acctSpan(r, action)
	ctx, cancel := fe.account.ctx(r)
	defer cancel()
	base := map[string]interface{}{"title": "Add address", "nav": "addresses", "address_id": id}
	if id != "" {
		base["title"] = "Edit address"
	}
	if r.Method == http.MethodGet {
		vals := map[string]string{}
		if id != "" {
			list, err := fe.account.client.ListAddresses(ctx, &pb.Empty{})
			if err != nil {
				fe.accountFailure(w, r, decodeAccountErr(err), "your saved addresses")
				return
			}
			found := false
			for _, a := range list.GetAddresses() {
				if a.GetId() == id {
					vals = map[string]string{"label": a.GetLabel(), "address_line1": a.GetLine1(), "address_line2": a.GetLine2(), "city": a.GetCity(), "state": a.GetState(), "zip": a.GetZip()}
					found = true
				}
			}
			if !found {
				fe.accountFailure(w, r, &accountErr{Code: codes.NotFound}, "that address")
				return
			}
		}
		base["values"] = vals
		fe.renderAcct(w, r, "acct_address_form", 0, base)
		return
	}
	if !checkPost(w, r) {
		return
	}
	addr := formAddress(r)
	label := strings.TrimSpace(r.FormValue("label"))
	var err error
	if id == "" {
		_, err = fe.account.client.CreateAddress(ctx, &pb.AddressInput{Label: label, Address: addr, MakeDefault: r.FormValue("make_default") == "on"})
	} else {
		_, err = fe.account.client.UpdateAddress(ctx, &pb.UpdateAddressRequest{Id: id, Label: label, Address: addr})
	}
	if err != nil {
		aerr := decodeAccountErr(err)
		if len(aerr.Fields) == 0 {
			if aerr.Reason == "address_limit_reached" {
				fe.renderAcct(w, r, "acct_address_form", http.StatusUnprocessableEntity, mergeMap(base, map[string]interface{}{"values": formValues(r, "label", "address_line1", "address_line2", "city", "state", "zip"),
					"error": "You've reached the limit of saved addresses. Delete one to add another."}))
				return
			}
			fe.accountFailure(w, r, aerr, "that address")
			return
		}
		acctOutcome(r, "rejected", aerr)
		fe.renderAcct(w, r, "acct_address_form", http.StatusUnprocessableEntity, mergeMap(base, map[string]interface{}{
			"values": formValues(r, "label", "address_line1", "address_line2", "city", "state", "zip"), "field_errors": fieldMessages(aerr)}))
		return
	}
	acctOutcome(r, "saved", nil)
	http.Redirect(w, r, baseUrl+"/account/addresses?notice=saved", http.StatusFound)
}

func mergeMap(a, b map[string]interface{}) map[string]interface{} {
	out := map[string]interface{}{}
	for k, v := range a {
		out[k] = v
	}
	for k, v := range b {
		out[k] = v
	}
	return out
}

func (fe *frontendServer) accountAddressAction(action string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		acctSpan(r, map[string]string{"delete": "address_delete", "default": "address_default"}[action])
		if !checkPost(w, r) {
			return
		}
		ctx, cancel := fe.account.ctx(r)
		defer cancel()
		id := mux.Vars(r)["id"]
		var err error
		if action == "delete" {
			_, err = fe.account.client.DeleteAddress(ctx, &pb.AddressRef{Id: id})
		} else {
			_, err = fe.account.client.SetDefaultAddress(ctx, &pb.AddressRef{Id: id})
		}
		if err != nil {
			fe.accountFailure(w, r, decodeAccountErr(err), "that address")
			return
		}
		acctOutcome(r, action+"d", nil)
		http.Redirect(w, r, baseUrl+"/account/addresses?notice="+action, http.StatusFound)
	}
}

// ---- orders and payment methods (data arrives with the order-history / payment milestones) ---

func (fe *frontendServer) accountOrders(w http.ResponseWriter, r *http.Request) {
	acctSpan(r, "orders_list")
	ctx, cancel := fe.account.ctx(r)
	defer cancel()
	resp, err := fe.account.client.ListOrders(ctx, &pb.ListOrdersRequest{PageSize: 20, Cursor: r.URL.Query().Get("cursor")})
	if err != nil {
		fe.accountFailure(w, r, decodeAccountErr(err), "your orders")
		return
	}
	fe.renderAcct(w, r, "acct_orders", 0, map[string]interface{}{"title": "Order history", "nav": "orders", "orders": resp.GetOrders(),
		"next_cursor": resp.GetNextCursor(), "pending": resp.GetQueueDepth() > 0})
}

func (fe *frontendServer) accountOrder(w http.ResponseWriter, r *http.Request) {
	acctSpan(r, "order_view")
	ctx, cancel := fe.account.ctx(r)
	defer cancel()
	order, err := fe.account.client.GetOrder(ctx, &pb.OrderRef{OrderId: mux.Vars(r)["id"]})
	if err != nil {
		fe.accountFailure(w, r, decodeAccountErr(err), "that order")
		return
	}
	fe.renderAcct(w, r, "acct_order", 0, map[string]interface{}{"title": "Order", "nav": "orders", "order": order})
}

func (fe *frontendServer) accountPayments(w http.ResponseWriter, r *http.Request) {
	acctSpan(r, "payment_list")
	fe.renderPayments(w, r, 0, "")
}

// renderPayments shows saved methods plus the synthetic demo catalog to add from. There is no card-number
// or CVV field anywhere: customers pick a catalog card and paymentservice tokenizes it.
func (fe *frontendServer) renderPayments(w http.ResponseWriter, r *http.Request, status int, errMsg string) {
	ctx, cancel := fe.account.ctx(r)
	defer cancel()
	resp, err := fe.account.client.ListPaymentMethods(ctx, &pb.Empty{})
	if err != nil {
		fe.accountFailure(w, r, decodeAccountErr(err), "your payment methods")
		return
	}
	data := map[string]interface{}{"title": "Payment methods", "nav": "payments", "methods": resp.GetMethods(),
		"notice": r.URL.Query().Get("notice"), "error": errMsg}
	if cat, err := fe.account.client.ListCatalogCards(ctx, &pb.Empty{}); err == nil {
		data["catalog"] = cat.GetCards()
	}
	fe.renderAcct(w, r, "acct_payments", status, data)
}

func (fe *frontendServer) accountPaymentAdd(w http.ResponseWriter, r *http.Request) {
	acctSpan(r, "payment_add")
	if !checkPost(w, r) {
		return
	}
	ctx, cancel := fe.account.ctx(r)
	defer cancel()
	_, err := fe.account.client.AddPaymentMethod(ctx, &pb.AddPaymentMethodRequest{
		CatalogId: r.FormValue("catalog_id"), MakeDefault: r.FormValue("make_default") == "on"})
	if err != nil {
		aerr := decodeAccountErr(err)
		acctOutcome(r, "failed", aerr)
		switch {
		case aerr.unavailable():
			fe.renderPayments(w, r, http.StatusServiceUnavailable, "Payment methods can't be added right now. Please try again shortly.")
		case aerr.Code == codes.ResourceExhausted:
			fe.renderPayments(w, r, http.StatusConflict, "You have reached the maximum number of saved payment methods.")
		case aerr.Code == codes.InvalidArgument:
			fe.renderPayments(w, r, http.StatusUnprocessableEntity, "Please choose one of the demo test cards.")
		default:
			fe.accountFailure(w, r, aerr, "your payment methods")
		}
		return
	}
	acctOutcome(r, "created", nil)
	http.Redirect(w, r, baseUrl+"/account/payment-methods?notice=added", http.StatusFound)
}

func (fe *frontendServer) accountPaymentAction(action string) http.HandlerFunc {
	return func(w http.ResponseWriter, r *http.Request) {
		acctSpan(r, map[string]string{"delete": "payment_delete", "default": "payment_default"}[action])
		if !checkPost(w, r) {
			return
		}
		ctx, cancel := fe.account.ctx(r)
		defer cancel()
		id := mux.Vars(r)["id"]
		var err error
		if action == "delete" {
			_, err = fe.account.client.DeletePaymentMethod(ctx, &pb.PaymentMethodRef{Id: id})
		} else {
			_, err = fe.account.client.SetDefaultPaymentMethod(ctx, &pb.PaymentMethodRef{Id: id})
		}
		if err != nil {
			fe.accountFailure(w, r, decodeAccountErr(err), "that payment method")
			return
		}
		acctOutcome(r, map[string]string{"delete": "deleted", "default": "updated"}[action], nil)
		http.Redirect(w, r, baseUrl+"/account/payment-methods?notice="+action, http.StatusFound)
	}
}

// accountDashboardEnabled is true when the gRPC account backend is configured (set in main).
var accountDashboardEnabled bool

// templateDict builds a map for passing several named values to a sub-template.
func templateDict(kv ...interface{}) map[string]interface{} {
	m := make(map[string]interface{}, len(kv)/2)
	for i := 0; i+1 < len(kv); i += 2 {
		if k, ok := kv[i].(string); ok {
			m[k] = kv[i+1]
		}
	}
	return m
}

// ---- checkout idempotency + failure presentation ------------------------------------------------

const cookieCheckoutKey = cookiePrefix + "checkout-key"

var reCheckoutKey = regexp.MustCompile(`^[A-Za-z0-9._:-]{8,80}$`)

// ensureCheckoutKey returns the sticky idempotency key for this browser's checkout attempt, creating it
// once. It survives refreshes, retries and returning to the cart after an unclear outcome, so the
// same order can never be charged twice; it is cleared on success or a definitive decline.
func ensureCheckoutKey(w http.ResponseWriter, r *http.Request) string {
	if v := cookieValue(r, cookieCheckoutKey); reCheckoutKey.MatchString(v) {
		return v
	}
	b := make([]byte, 18)
	_, _ = cryptorand.Read(b)
	v := "co-" + base64.RawURLEncoding.EncodeToString(b)
	http.SetCookie(w, &http.Cookie{Name: cookieCheckoutKey, Value: v, Path: "/", MaxAge: 3600, HttpOnly: true, SameSite: http.SameSiteLaxMode, Secure: secureRequest(r)})
	r.AddCookie(&http.Cookie{Name: cookieCheckoutKey, Value: v})
	return v
}

func clearCheckoutKey(w http.ResponseWriter, r *http.Request) {
	http.SetCookie(w, &http.Cookie{Name: cookieCheckoutKey, Value: "", Path: "/", MaxAge: -1, HttpOnly: true, SameSite: http.SameSiteLaxMode})
}

// withAccountSession forwards the opaque account session so checkoutservice can resolve the customer.
func withAccountSession(r *http.Request) context.Context {
	ctx := r.Context()
	if tok := cookieValue(r, cookieAuth); tok != "" {
		ctx = metadata.AppendToOutgoingContext(ctx, "x-account-session", tok)
	}
	return ctx
}

// reasonOf extracts the stable reason code checkoutservice puts in status messages ("reason:<code>").
func reasonOf(err error) string {
	if st, ok := status.FromError(err); ok {
		if m := st.Message(); strings.HasPrefix(m, "reason:") {
			return strings.TrimPrefix(m, "reason:")
		}
	}
	return ""
}

func (fe *frontendServer) checkoutFailure(w http.ResponseWriter, r *http.Request, log logrus.FieldLogger, err error) {
	reason := reasonOf(err)
	code, msg := http.StatusInternalServerError, "We couldn't complete your order. Please try again."
	switch reason {
	case "payment_declined":
		clearCheckoutKey(w, r) // definitive: a corrected card is a new attempt with a new key
		code, msg = http.StatusPaymentRequired, "Your card was declined. Nothing was charged. Please check your card details and try again."
	case "payment_uncertain":
		code, msg = http.StatusServiceUnavailable, "We couldn't confirm your payment yet. Please wait a moment and place the order again from your cart: you will not be charged twice."
	case "checkout_in_progress":
		code, msg = http.StatusConflict, "Your order is already being processed. Please wait a moment instead of submitting again, then check your order history."
	case "idempotency_conflict":
		code, msg = http.StatusConflict, "We're still finishing your previous checkout attempt. Please wait a moment, then try again with the same details."
	case "shipping_unavailable":
		code, msg = http.StatusServiceUnavailable, "Your payment went through and we're finishing your order. Please place the order again in a moment: you will not be charged twice."
	case "order_queue_unavailable":
		code, msg = http.StatusServiceUnavailable, "Checkout is temporarily unavailable and you have not been charged. Please try again shortly."
	case "cart_empty":
		code, msg = http.StatusUnprocessableEntity, "Your cart is empty."
	case "cart_unavailable", "catalog_unavailable", "currency_unavailable":
		code, msg = http.StatusServiceUnavailable, "Checkout is temporarily unavailable. Please try again shortly."
	}
	log.WithField("reason", reason).Warn("checkout failed")
	setAppAttrs(trace.SpanFromContext(r.Context()), attribute.String("app.account.outcome", firstReason(reason)))
	renderHTTPError(log, r, w, errors.New(msg), code)
}

func firstReason(reason string) string {
	if reCode.MatchString(reason) {
		return reason
	}
	return "error"
}

func renderDate(unix int64) string {
	if unix <= 0 {
		return ""
	}
	return time.Unix(unix, 0).UTC().Format("Jan 2, 2006")
}

// claimGuestOrders attaches orders this browser placed as a guest to the account that just signed in.
// Best effort: history is a convenience, so a failure never blocks sign-in.
func (fe *frontendServer) claimGuestOrders(r *http.Request, token string) {
	if token == "" || sessionID(r) == "" {
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), accountCallTimeout)
	defer cancel()
	ctx = metadata.NewOutgoingContext(ctx, metadata.Pairs("x-service-name", "frontend", "x-service-secret", fe.account.secret,
		"x-account-session", token))
	resp, err := fe.account.client.ClaimGuestOrders(ctx, &pb.ClaimGuestRequest{SessionId: sessionID(r)})
	span := trace.SpanFromContext(r.Context())
	if err != nil {
		span.SetAttributes(attribute.String("app.account.guest_claim", "failed"))
		return
	}
	span.SetAttributes(attribute.Int("app.account.guest_orders_claimed", int(resp.GetClaimed())))
}

// savedMethods lists the signed-in customer's saved demo cards for the checkout form. Best effort: if
// accountservice is unavailable the form simply offers the card fields.
func (fe *frontendServer) savedMethods(r *http.Request) []*pb.PaymentMethod {
	if fe.account == nil || !accountFromContext(r.Context()).Authenticated {
		return nil
	}
	ctx, cancel := fe.account.ctx(r)
	defer cancel()
	resp, err := fe.account.client.ListPaymentMethods(ctx, &pb.Empty{})
	if err != nil {
		return nil
	}
	return resp.GetMethods()
}

// cardForRequest: with a saved method the request carries NO card data at all.
func cardForRequest(p validator.PlaceOrderPayload, savedMethodID string) *pb.CreditCardInfo {
	if savedMethodID != "" {
		return nil
	}
	return &pb.CreditCardInfo{CreditCardNumber: p.CcNumber, CreditCardExpirationMonth: int32(p.CcMonth),
		CreditCardExpirationYear: int32(p.CcYear), CreditCardCvv: int32(p.CcCVV)}
}
