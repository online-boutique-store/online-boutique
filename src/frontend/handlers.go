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
	cryptorand "crypto/rand"
	"encoding/base64"
	"encoding/json"
	"fmt"
	"html/template"
	"io"
	"math/rand"
	"net"
	"net/http"
	"os"
	"strconv"
	"strings"
	"time"

	"github.com/gorilla/mux"
	"github.com/pkg/errors"
	"github.com/sirupsen/logrus"
	"go.opentelemetry.io/contrib/instrumentation/net/http/otelhttp"

	pb "github.com/GoogleCloudPlatform/microservices-demo/src/frontend/genproto"
	"github.com/GoogleCloudPlatform/microservices-demo/src/frontend/money"
	"github.com/GoogleCloudPlatform/microservices-demo/src/frontend/validator"
)

type platformDetails struct {
	css      string
	provider string
}

var (
	frontendMessage  = strings.TrimSpace(os.Getenv("FRONTEND_MESSAGE"))
	isCymbalBrand    = "true" == strings.ToLower(os.Getenv("CYMBAL_BRANDING"))
	assistantEnabled = "true" == strings.ToLower(os.Getenv("ENABLE_ASSISTANT"))
	templates        = template.Must(template.New("").
				Funcs(template.FuncMap{
			"renderMoney":        renderMoney,
			"renderCurrencyLogo": renderCurrencyLogo,
			"dict":               templateDict,
			"date":               renderDate,
		}).ParseGlob("templates/*.html"))
	plat platformDetails
)

var validEnvs = []string{"local", "gcp", "azure", "aws", "onprem", "alibaba"}

func (fe *frontendServer) homeHandler(w http.ResponseWriter, r *http.Request) {
	log := r.Context().Value(ctxKeyLog{}).(logrus.FieldLogger)
	log.WithField("currency", currentCurrency(r)).Info("home")
	currencies, err := fe.getCurrencies(r.Context())
	if err != nil {
		renderHTTPError(log, r, w, errors.Wrap(err, "could not retrieve currencies"), http.StatusInternalServerError)
		return
	}
	products, err := fe.getProducts(r.Context())
	if err != nil {
		renderHTTPError(log, r, w, errors.Wrap(err, "could not retrieve products"), http.StatusInternalServerError)
		return
	}
	cart, err := fe.getCart(r.Context(), sessionID(r))
	if err != nil {
		renderHTTPError(log, r, w, errors.Wrap(err, "could not retrieve cart"), http.StatusInternalServerError)
		return
	}

	type productView struct {
		Item  *pb.Product
		Price *pb.Money
	}
	ps := make([]productView, len(products))
	for i, p := range products {
		price, err := fe.convertCurrency(r.Context(), p.GetPriceUsd(), currentCurrency(r))
		if err != nil {
			renderHTTPError(log, r, w, errors.Wrapf(err, "failed to do currency conversion for product %s", p.GetId()), http.StatusInternalServerError)
			return
		}
		ps[i] = productView{p, price}
	}

	// Set ENV_PLATFORM (default to local if not set; use env var if set; otherwise detect GCP, which overrides env)_
	var env = os.Getenv("ENV_PLATFORM")
	// Only override from env variable if set + valid env
	if env == "" || stringinSlice(validEnvs, env) == false {
		fmt.Println("env platform is either empty or invalid")
		env = "local"
	}
	// Autodetect GCP
	addrs, err := net.LookupHost("metadata.google.internal.")
	if err == nil && len(addrs) >= 0 {
		log.Debugf("Detected Google metadata server: %v, setting ENV_PLATFORM to GCP.", addrs)
		env = "gcp"
	}

	log.Debugf("ENV_PLATFORM is: %s", env)
	plat = platformDetails{}
	plat.setPlatformDetails(strings.ToLower(env))

	if err := templates.ExecuteTemplate(w, "home", injectCommonTemplateData(r, map[string]interface{}{
		"show_currency": true,
		"currencies":    currencies,
		"products":      ps,
		"cart_size":     cartSize(cart),
		"banner_color":  os.Getenv("BANNER_COLOR"), // illustrates canary deployments
		"ad":            fe.chooseAd(r.Context(), []string{}, log),
	})); err != nil {
		log.Error(err)
	}
}

func (plat *platformDetails) setPlatformDetails(env string) {
	if env == "aws" {
		plat.provider = "AWS"
		plat.css = "aws-platform"
	} else if env == "onprem" {
		plat.provider = "On-Premises"
		plat.css = "onprem-platform"
	} else if env == "azure" {
		plat.provider = "Azure"
		plat.css = "azure-platform"
	} else if env == "gcp" {
		plat.provider = "Google Cloud"
		plat.css = "gcp-platform"
	} else if env == "alibaba" {
		plat.provider = "Alibaba Cloud"
		plat.css = "alibaba-platform"
	} else {
		plat.provider = "local"
		plat.css = "local"
	}
}

func (fe *frontendServer) productHandler(w http.ResponseWriter, r *http.Request) {
	log := r.Context().Value(ctxKeyLog{}).(logrus.FieldLogger)
	id := mux.Vars(r)["id"]
	if id == "" {
		renderHTTPError(log, r, w, errors.New("product id not specified"), http.StatusBadRequest)
		return
	}
	log.WithField("id", id).WithField("currency", currentCurrency(r)).
		Debug("serving product page")

	// Seed app.product.id into baggage so downstream productcatalog /
	// recommendation spans carry it.
	r = r.WithContext(withBaggage(r.Context(), "app.product.id", id))
	stampAppAttrs(r.Context())

	p, err := fe.getProduct(r.Context(), id)
	if err != nil {
		renderHTTPError(log, r, w, errors.Wrap(err, "could not retrieve product"), http.StatusInternalServerError)
		return
	}
	currencies, err := fe.getCurrencies(r.Context())
	if err != nil {
		renderHTTPError(log, r, w, errors.Wrap(err, "could not retrieve currencies"), http.StatusInternalServerError)
		return
	}

	cart, err := fe.getCart(r.Context(), sessionID(r))
	if err != nil {
		renderHTTPError(log, r, w, errors.Wrap(err, "could not retrieve cart"), http.StatusInternalServerError)
		return
	}

	price, err := fe.convertCurrency(r.Context(), p.GetPriceUsd(), currentCurrency(r))
	if err != nil {
		renderHTTPError(log, r, w, errors.Wrap(err, "failed to convert currency"), http.StatusInternalServerError)
		return
	}

	// ignores the error retrieving recommendations since it is not critical
	recommendations, err := fe.getRecommendations(r.Context(), sessionID(r), []string{id})
	if err != nil {
		log.WithField("error", err).Warn("failed to get product recommendations")
	}

	product := struct {
		Item  *pb.Product
		Price *pb.Money
	}{p, price}

	// Fetch packaging info (weight/dimensions) of the product
	// The packaging service is an optional microservice you can run as part of a Google Cloud demo.
	var packagingInfo *PackagingInfo = nil
	if isPackagingServiceConfigured() {
		packagingInfo, err = httpGetPackagingInfo(id)
		if err != nil {
			fmt.Println("Failed to obtain product's packaging info:", err)
		}
	}

	if err := templates.ExecuteTemplate(w, "product", injectCommonTemplateData(r, map[string]interface{}{
		"ad":              fe.chooseAd(r.Context(), p.Categories, log),
		"show_currency":   true,
		"currencies":      currencies,
		"product":         product,
		"recommendations": recommendations,
		"cart_size":       cartSize(cart),
		"packagingInfo":   packagingInfo,
	})); err != nil {
		log.Println(err)
	}
}

func (fe *frontendServer) addToCartHandler(w http.ResponseWriter, r *http.Request) {
	log := r.Context().Value(ctxKeyLog{}).(logrus.FieldLogger)
	quantity, _ := strconv.ParseUint(r.FormValue("quantity"), 10, 32)
	productID := r.FormValue("product_id")
	payload := validator.AddToCartPayload{
		Quantity:  quantity,
		ProductID: productID,
	}
	if err := payload.Validate(); err != nil {
		renderHTTPError(log, r, w, validator.ValidationErrorResponse(err), http.StatusUnprocessableEntity)
		return
	}
	log.WithField("product", payload.ProductID).WithField("quantity", payload.Quantity).Debug("adding to cart")

	// Seed the cart operation + product attributes into baggage so downstream
	// cart / recommendation spans carry them.
	ctx := withBaggage(r.Context(), "app.product.id", payload.ProductID)
	ctx = withBaggage(ctx, "app.product.quantity", strconv.FormatUint(payload.Quantity, 10))
	ctx = withBaggage(ctx, "app.cart.action", "add")
	r = r.WithContext(ctx)
	stampAppAttrs(ctx)

	p, err := fe.getProduct(r.Context(), payload.ProductID)
	if err != nil {
		renderHTTPError(log, r, w, errors.Wrap(err, "could not retrieve product"), http.StatusInternalServerError)
		return
	}

	if err := fe.insertCart(r.Context(), sessionID(r), p.GetId(), int32(payload.Quantity)); err != nil {
		renderHTTPError(log, r, w, errors.Wrap(err, "failed to add to cart"), http.StatusInternalServerError)
		return
	}
	w.Header().Set("location", baseUrl+"/cart")
	w.WriteHeader(http.StatusFound)
}

func (fe *frontendServer) emptyCartHandler(w http.ResponseWriter, r *http.Request) {
	log := r.Context().Value(ctxKeyLog{}).(logrus.FieldLogger)
	log.Debug("emptying cart")

	r = r.WithContext(withBaggage(r.Context(), "app.cart.action", "empty"))
	stampAppAttrs(r.Context())

	if err := fe.emptyCart(r.Context(), sessionID(r)); err != nil {
		renderHTTPError(log, r, w, errors.Wrap(err, "failed to empty cart"), http.StatusInternalServerError)
		return
	}
	w.Header().Set("location", baseUrl+"/")
	w.WriteHeader(http.StatusFound)
}

func (fe *frontendServer) viewCartHandler(w http.ResponseWriter, r *http.Request) {
	ensureCheckoutKey(w, r)
	log := r.Context().Value(ctxKeyLog{}).(logrus.FieldLogger)
	log.Debug("view user cart")

	r = r.WithContext(withBaggage(r.Context(), "app.cart.action", "view"))
	stampAppAttrs(r.Context())

	currencies, err := fe.getCurrencies(r.Context())
	if err != nil {
		renderHTTPError(log, r, w, errors.Wrap(err, "could not retrieve currencies"), http.StatusInternalServerError)
		return
	}
	cart, err := fe.getCart(r.Context(), sessionID(r))
	if err != nil {
		renderHTTPError(log, r, w, errors.Wrap(err, "could not retrieve cart"), http.StatusInternalServerError)
		return
	}

	// ignores the error retrieving recommendations since it is not critical
	recommendations, err := fe.getRecommendations(r.Context(), sessionID(r), cartIDs(cart))
	if err != nil {
		log.WithField("error", err).Warn("failed to get product recommendations")
	}

	shippingCost, err := fe.getShippingQuote(r.Context(), cart, currentCurrency(r))
	if err != nil {
		renderHTTPError(log, r, w, errors.Wrap(err, "failed to get shipping quote"), http.StatusInternalServerError)
		return
	}

	type cartItemView struct {
		Item     *pb.Product
		Quantity int32
		Price    *pb.Money
	}
	items := make([]cartItemView, len(cart))
	totalPrice := pb.Money{CurrencyCode: currentCurrency(r)}
	for i, item := range cart {
		p, err := fe.getProduct(r.Context(), item.GetProductId())
		if err != nil {
			renderHTTPError(log, r, w, errors.Wrapf(err, "could not retrieve product #%s", item.GetProductId()), http.StatusInternalServerError)
			return
		}
		price, err := fe.convertCurrency(r.Context(), p.GetPriceUsd(), currentCurrency(r))
		if err != nil {
			renderHTTPError(log, r, w, errors.Wrapf(err, "could not convert currency for product #%s", item.GetProductId()), http.StatusInternalServerError)
			return
		}

		multPrice := money.MultiplySlow(*price, uint32(item.GetQuantity()))
		items[i] = cartItemView{
			Item:     p,
			Quantity: item.GetQuantity(),
			Price:    &multPrice}
		totalPrice = money.Must(money.Sum(totalPrice, multPrice))
	}
	totalPrice = money.Must(money.Sum(totalPrice, *shippingCost))
	// Pre-checkout cart dollar value — see stampCartValueAttrs in
	// otelattrs.go for why this is a direct span attribute, not baggage.
	stampCartValueAttrs(r.Context(), totalPrice, len(cart))
	year := time.Now().Year()

	if err := templates.ExecuteTemplate(w, "cart", injectCommonTemplateData(r, map[string]interface{}{
		"currencies":       currencies,
		"recommendations":  recommendations,
		"cart_size":        cartSize(cart),
		"shipping_cost":    shippingCost,
		"show_currency":    true,
		"total_cost":       totalPrice,
		"items":            items,
		"expiration_years": []int{year, year + 1, year + 2, year + 3, year + 4},
		"saved_methods":    fe.savedMethods(r),
	})); err != nil {
		log.Println(err)
	}
}

func (fe *frontendServer) placeOrderHandler(w http.ResponseWriter, r *http.Request) {
	log := r.Context().Value(ctxKeyLog{}).(logrus.FieldLogger)
	log.Debug("placing order")

	var (
		email         = r.FormValue("email")
		streetAddress = r.FormValue("street_address")
		zipCode, _    = strconv.ParseInt(r.FormValue("zip_code"), 10, 32)
		city          = r.FormValue("city")
		state         = r.FormValue("state")
		country       = r.FormValue("country")
		ccNumber      = r.FormValue("credit_card_number")
		ccMonth, _    = strconv.ParseInt(r.FormValue("credit_card_expiration_month"), 10, 32)
		ccYear, _     = strconv.ParseInt(r.FormValue("credit_card_expiration_year"), 10, 32)
		ccCVV, _      = strconv.ParseInt(r.FormValue("credit_card_cvv"), 10, 32)
	)

	savedMethodID := strings.TrimSpace(r.FormValue("payment_method_id"))
	payload := validator.PlaceOrderPayload{
		Email:         email,
		StreetAddress: streetAddress,
		ZipCode:       zipCode,
		City:          city,
		State:         state,
		Country:       country,
		CcNumber:      ccNumber,
		CcMonth:       ccMonth,
		CcYear:        ccYear,
		CcCVV:         ccCVV,
	}
	if savedMethodID != "" {
		// A saved demo card is charged by token inside checkout; no card fields are submitted or validated.
		payload.CcNumber, payload.CcMonth, payload.CcYear, payload.CcCVV = "4111111111111111", 1, 2099, 123
	}
	if err := payload.Validate(); err != nil {
		renderHTTPError(log, r, w, validator.ValidationErrorResponse(err), http.StatusUnprocessableEntity)
		return
	}

	// Seed the cart operation into baggage so checkout and all downstream
	// spans (payment, shipping, email) carry app.cart.action=checkout.
	ctx := withBaggage(r.Context(), "app.cart.action", "checkout")
	r = r.WithContext(ctx)
	stampAppAttrs(ctx)

	key := ensureCheckoutKey(w, r)
	order, err := pb.NewCheckoutServiceClient(fe.checkoutSvcConn).
		PlaceOrder(withAccountSession(r), &pb.PlaceOrderRequest{
			Email:           payload.Email,
			CreditCard:      cardForRequest(payload, savedMethodID),
			PaymentMethodId: savedMethodID,
			UserId:          sessionID(r),
			UserCurrency:    currentCurrency(r),
			IdempotencyKey:  key,
			Channel:         "web",
			Address: &pb.Address{
				StreetAddress: payload.StreetAddress,
				City:          payload.City,
				State:         payload.State,
				ZipCode:       int32(payload.ZipCode),
				PostalCode:    fmt.Sprintf("%05d", payload.ZipCode),
				Country:       payload.Country},
		})
	if err != nil {
		// Capture what this failed checkout was worth before returning the
		// error — see stampFailedCheckoutValue in otelattrs.go.
		fe.stampFailedCheckoutValue(r.Context(), sessionID(r), currentCurrency(r))
		fe.checkoutFailure(w, r, log, err)
		return
	}
	clearCheckoutKey(w, r)
	log.WithField("order", order.GetOrder().GetOrderId()).Info("order placed")

	// Backfill the order identity onto the root frontend span and into baggage so the trailing
	// getRecommendations call carries it.
	r = r.WithContext(withBaggage(r.Context(), "app.order.id", order.GetOrder().GetOrderId()))
	var purchasedIDs []string
	for _, v := range order.GetOrder().GetItems() {
		purchasedIDs = append(purchasedIDs, v.GetItem().GetProductId())
	}
	stampAppAttrs(r.Context())

	recommendations, _ := fe.getRecommendations(r.Context(), sessionID(r), nil)

	totalPaid := *order.GetOrder().GetShippingCost()
	for _, v := range order.GetOrder().GetItems() {
		multPrice := money.MultiplySlow(*v.GetCost(), uint32(v.GetItem().GetQuantity()))
		totalPaid = money.Must(money.Sum(totalPaid, multPrice))
	}

	currencies, err := fe.getCurrencies(r.Context())
	if err != nil {
		renderHTTPError(log, r, w, errors.Wrap(err, "could not retrieve currencies"), http.StatusInternalServerError)
		return
	}

	if err := templates.ExecuteTemplate(w, "order", injectCommonTemplateData(r, map[string]interface{}{
		"show_currency":   false,
		"currencies":      currencies,
		"order":           order.GetOrder(),
		"record_status":   order.GetRecordStatus(),
		"total_paid":      &totalPaid,
		"recommendations": recommendations,
	})); err != nil {
		log.Println(err)
	}
}

func (fe *frontendServer) assistantHandler(w http.ResponseWriter, r *http.Request) {
	currencies, err := fe.getCurrencies(r.Context())
	if err != nil {
		renderHTTPError(log, r, w, errors.Wrap(err, "could not retrieve currencies"), http.StatusInternalServerError)
		return
	}

	if err := templates.ExecuteTemplate(w, "assistant", injectCommonTemplateData(r, map[string]interface{}{
		"show_currency": false,
		"currencies":    currencies,
	})); err != nil {
		log.Println(err)
	}
}

func (fe *frontendServer) accountActivationHandler(w http.ResponseWriter, r *http.Request) {
	if fe.account != nil {
		fe.grpcActivation(w, r)
		return
	}
	if r.Method == http.MethodPost {
		fe.accountSubmit(w, r, "activation")
		return
	}
	fe.renderAccountForm(w, r, "Complete Account Setup", "activation")
}

func (fe *frontendServer) accountCreateHandler(w http.ResponseWriter, r *http.Request) {
	if fe.account != nil {
		fe.grpcSignup(w, r)
		return
	}
	if r.Method == http.MethodPost {
		fe.accountSubmit(w, r, "create")
		return
	}
	fe.renderAccountForm(w, r, "Create Account", "create")
}

func (fe *frontendServer) loginHandler(w http.ResponseWriter, r *http.Request) {
	if fe.account != nil {
		fe.grpcLogin(w, r)
		return
	}
	if r.Method == http.MethodPost {
		fe.accountSubmit(w, r, "login")
		return
	}
	fe.renderAccountForm(w, r, "Log In", "login")
}

func (fe *frontendServer) accountProfileHandler(w http.ResponseWriter, r *http.Request) {
	if fe.account != nil {
		fe.requireLogin(fe.accountProfile)(w, r)
		return
	}
	if r.Method == http.MethodPost {
		fe.accountSubmit(w, r, "profile")
		return
	}
	csrf, _ := r.Cookie(cookieCSRF)
	if csrf == nil {
		value := make([]byte, 24)
		_, _ = cryptorand.Read(value)
		csrf = &http.Cookie{Name: cookieCSRF, Value: base64.RawURLEncoding.EncodeToString(value), Path: "/", MaxAge: cookieMaxAge, HttpOnly: true, SameSite: http.SameSiteLaxMode}
		http.SetCookie(w, csrf)
	}
	profile, status, err := fe.accountAPIGet(r, "/account/profile")
	if err != nil || status >= http.StatusInternalServerError {
		// Keep the auth cookie: the customer is still logged in, only their saved details are unavailable.
		renderAccountTemplate(w, r, "My Account", "unavailable", csrf.Value, nil, "Your saved account details are temporarily unavailable. Please try again shortly.")
		return
	}
	if status >= 400 {
		http.Redirect(w, r, baseUrl+"/login", http.StatusFound)
		return
	}
	_ = templates.ExecuteTemplate(w, "account", injectCommonTemplateData(r, map[string]interface{}{"title": "My Account", "mode": "profile", "csrf": csrf.Value, "account": profile["account"]}))
}

func (fe *frontendServer) accountSubmit(w http.ResponseWriter, r *http.Request, mode string) {
	payload := map[string]interface{}{}
	for _, key := range []string{"first_name", "last_name", "email", "password", "street_address", "city", "state", "zip_code", "country", "preferred_currency"} {
		payload[key] = strings.TrimSpace(r.FormValue(key))
	}
	if csrf, _ := r.Cookie(cookieCSRF); csrf == nil || csrf.Value != r.FormValue("csrf") {
		http.Error(w, "invalid request", http.StatusForbidden)
		return
	}
	if mode == "profile" {
		payload["session_token"] = cookieValue(r, cookieAuth)
		payload["address"] = map[string]interface{}{"street_address": payload["street_address"], "city": payload["city"], "state": payload["state"], "zip_code": payload["zip_code"], "country": payload["country"]}
	}
	path := map[string]string{"create": "/account/register", "login": "/account/login", "profile": "/account/profile", "activation": "/account/claim"}[mode]
	if mode == "activation" {
		// The activation code never reaches the browser: the assistant resolves it server-side
		// from this anonymous session id.
		payload["session_id"] = sessionID(r)
		clearActivationCookies(w)
	}
	result, status, err := fe.accountAPI(r, path, payload)
	if err != nil || status >= 400 {
		csrf, _ := r.Cookie(cookieCSRF)
		values := map[string]string{}
		for _, key := range []string{"first_name", "last_name", "email", "street_address", "city", "state", "zip_code", "country", "preferred_currency"} {
			values[key] = r.FormValue(key)
		}
		titles := map[string]string{"create": "Create Account", "login": "Log In", "profile": "My Account", "activation": "Complete Account Setup"}
		renderAccountTemplate(w, r, titles[mode], mode, csrf.Value, values, accountErrorMessage(result, status, err))
		return
	}
	if token, ok := result["session_token"].(string); ok && token != "" {
		http.SetCookie(w, &http.Cookie{Name: cookieAuth, Value: token, Path: "/", MaxAge: cookieMaxAge, HttpOnly: true, SameSite: http.SameSiteLaxMode})
	} else if login, _ := result["login_required"].(bool); login {
		w.Header().Set("Location", baseUrl+"/login?notice=account_ready")
		w.WriteHeader(http.StatusFound)
		return
	}
	w.Header().Set("Location", baseUrl+"/account/profile")
	w.WriteHeader(http.StatusFound)
}

func cookieValue(r *http.Request, name string) string {
	c, _ := r.Cookie(name)
	if c == nil {
		return ""
	}
	return c.Value
}

func (fe *frontendServer) renderAccountForm(w http.ResponseWriter, r *http.Request, title, mode string) {
	csrf, _ := r.Cookie(cookieCSRF)
	if csrf == nil {
		value := make([]byte, 24)
		_, _ = cryptorand.Read(value)
		csrf = &http.Cookie{Name: cookieCSRF, Value: base64.RawURLEncoding.EncodeToString(value), Path: "/", MaxAge: cookieMaxAge, HttpOnly: true, SameSite: http.SameSiteLaxMode}
		http.SetCookie(w, csrf)
	}
	if mode == "activation" {
		clearActivationCookies(w) // leftovers from builds that stored the code in the browser
		activation, status, activationErr := fe.accountAPIGet(r, "/account/activation")
		if status == http.StatusNotFound {
			renderAccountTemplate(w, r, title, mode, csrf.Value, nil, "There is no account setup in progress for this browser.")
			return
		}
		if activationErr != nil || status >= 400 {
			renderAccountTemplate(w, r, title, mode, csrf.Value, nil, "Account setup is temporarily unavailable. Please try again.")
			return
		}
		email, _ := activation["email"].(string)
		renderAccountTemplate(w, r, title, mode, csrf.Value, map[string]string{"email": email}, "")
		return
	}
	notice := ""
	if mode == "login" && r.URL.Query().Get("notice") == "account_ready" {
		notice = "Your account is ready - please log in."
	}
	renderAccountTemplate(w, r, title, mode, csrf.Value, nil, notice)
}

func clearActivationCookies(w http.ResponseWriter) {
	for _, name := range []string{"shop_activation_email", "shop_activation_code"} {
		http.SetCookie(w, &http.Cookie{Name: name, Value: "", Path: "/", MaxAge: -1, HttpOnly: true, SameSite: http.SameSiteLaxMode})
	}
}

func renderAccountTemplate(w http.ResponseWriter, r *http.Request, title, mode, csrf string, values map[string]string, accountError string) {
	if values == nil {
		values = map[string]string{}
	}
	_ = templates.ExecuteTemplate(w, "account", injectCommonTemplateData(r, map[string]interface{}{
		"title": title, "mode": mode, "csrf": csrf, "account_values": values, "account_error": accountError,
	}))
}

func accountErrorMessage(result map[string]interface{}, status int, err error) string {
	if err != nil || status >= http.StatusInternalServerError {
		return "The account service is temporarily unavailable. Please try again."
	}
	switch status {
	case http.StatusConflict:
		return "An account with that email already exists. Try logging in instead."
	case http.StatusUnauthorized:
		return "The email or password is incorrect."
	case http.StatusTooManyRequests:
		return "Too many login attempts. Please try again later."
	case http.StatusBadRequest:
		if result != nil {
			if message, ok := result["error"].(string); ok && message == "validation_failed" {
				if fields, ok := result["fields"].(map[string]interface{}); ok && len(fields) > 0 {
					order := []string{"first_name", "last_name", "email", "password", "confirm_password", "street_address", "city", "state", "zip_code", "country", "preferred_currency"}
					var msgs []string
					for _, key := range order {
						if m, ok := fields[key].(string); ok && m != "" {
							msgs = append(msgs, m)
						}
					}
					if len(msgs) > 0 {
						return strings.Join(msgs, " ")
					}
				}
				return "Please check the account details and try again."
			}
		}
	}
	return "The account request could not be completed. Please try again."
}

func (fe *frontendServer) accountAPIGet(r *http.Request, path string) (map[string]interface{}, int, error) {
	req, err := http.NewRequestWithContext(r.Context(), http.MethodGet, "http://"+fe.shoppingAssistantSvcAddr+path, nil)
	if err != nil {
		return nil, 0, err
	}
	req.Header.Set("X-Account-Api-Secret", fe.accountAPISecret)
	req.Header.Set("X-Session-ID", sessionID(r))
	if auth := cookieValue(r, cookieAuth); auth != "" {
		req.Header.Set("X-Account-Session", auth)
	}
	res, err := assistantClient.Do(req)
	if err != nil {
		return nil, 0, err
	}
	defer res.Body.Close()
	var out map[string]interface{}
	err = json.NewDecoder(res.Body).Decode(&out)
	return out, res.StatusCode, err
}

func (fe *frontendServer) accountAPI(r *http.Request, path string, payload map[string]interface{}) (map[string]interface{}, int, error) {
	body, err := json.Marshal(payload)
	if err != nil {
		return nil, 0, err
	}
	req, err := http.NewRequestWithContext(r.Context(), http.MethodPost, "http://"+fe.shoppingAssistantSvcAddr+path, strings.NewReader(string(body)))
	if err != nil {
		return nil, 0, err
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("X-Account-Api-Secret", fe.accountAPISecret)
	req.Header.Set("X-Session-ID", sessionID(r))
	if auth := cookieValue(r, cookieAuth); auth != "" {
		req.Header.Set("X-Account-Session", auth)
	}
	res, err := assistantClient.Do(req)
	if err != nil {
		return nil, 0, err
	}
	defer res.Body.Close()
	var out map[string]interface{}
	err = json.NewDecoder(res.Body).Decode(&out)
	return out, res.StatusCode, err
}

func (fe *frontendServer) logoutHandler(w http.ResponseWriter, r *http.Request) {
	if fe.account != nil {
		fe.grpcLogout(w, r)
		return
	}
	log := r.Context().Value(ctxKeyLog{}).(logrus.FieldLogger)
	log.Debug("logging out")
	if c, err := r.Cookie(cookieAuth); err == nil {
		_, _, _ = fe.accountAPI(r, "/account/logout", nil)
		http.SetCookie(w, &http.Cookie{Name: c.Name, Value: "", Path: "/", Expires: time.Now().Add(-time.Hour * 24 * 365),
			MaxAge: -1, HttpOnly: true, SameSite: http.SameSiteLaxMode})
	}
	w.Header().Set("Location", baseUrl+"/")
	w.WriteHeader(http.StatusFound)
}

func (fe *frontendServer) getProductByID(w http.ResponseWriter, r *http.Request) {
	id := mux.Vars(r)["ids"]
	if id == "" {
		return
	}

	p, err := fe.getProduct(r.Context(), id)
	if err != nil {
		return
	}

	jsonData, err := json.Marshal(p)
	if err != nil {
		fmt.Println(err)
		return
	}

	w.Write(jsonData)
	w.WriteHeader(http.StatusOK)
}

// assistantClient carries the request's trace context and baggage (app.session.id,
// etc.) to the shopping assistant, so its spans join the frontend's trace.
var assistantClient = &http.Client{
	Transport: otelhttp.NewTransport(http.DefaultTransport),
	Timeout:   90 * time.Second,
}

func (fe *frontendServer) chatBotHandler(w http.ResponseWriter, r *http.Request) {
	log := r.Context().Value(ctxKeyLog{}).(logrus.FieldLogger)
	type CheckoutInfo struct {
		Stage         string   `json:"stage"`
		NextStep      string   `json:"next_step"`
		Missing       []string `json:"missing"`
		CheckoutID    string   `json:"checkout_id"`
		Customer      string   `json:"customer_type"`
		CompleteSetup bool     `json:"complete_setup"`
	}
	type Response struct {
		Message       string        `json:"message"`
		TraceID       string        `json:"trace_id,omitempty"`
		Outcome       string        `json:"outcome,omitempty"`
		Checkout      *CheckoutInfo `json:"checkout,omitempty"`
		CompleteSetup bool          `json:"complete_setup,omitempty"`
	}

	type LLMResponse struct {
		Content  string        `json:"content"`
		TraceID  string        `json:"trace_id"`
		Checkout *CheckoutInfo `json:"checkout"`
		Details  struct {
			Outcome string `json:"outcome"`
		} `json:"details"`
	}

	var response LLMResponse

	url := "http://" + fe.shoppingAssistantSvcAddr
	req, err := http.NewRequestWithContext(r.Context(), http.MethodPost, url, r.Body)
	if err != nil {
		renderHTTPError(log, r, w, errors.Wrap(err, "failed to create request"), http.StatusInternalServerError)
		return
	}
	req.Header.Set("Content-Type", "application/json")
	req.Header.Set("Accept", "application/json")
	req.Header.Set("X-Session-ID", sessionID(r))
	if auth := cookieValue(r, cookieAuth); auth != "" {
		req.Header.Set("X-Account-Session", auth)
	}
	for _, h := range []string{"X-Prompt-Category", "X-Conversation-ID"} {
		if v := r.Header.Get(h); v != "" {
			req.Header.Set(h, v)
		}
	}
	res, err := assistantClient.Do(req)
	if err != nil {
		renderHTTPError(log, r, w, errors.Wrap(err, "failed to send request"), http.StatusInternalServerError)
		return
	}
	defer res.Body.Close()

	body, err := io.ReadAll(res.Body)
	if err != nil {
		renderHTTPError(log, r, w, errors.Wrap(err, "failed to read response"), http.StatusInternalServerError)
		return
	}

	err = json.Unmarshal(body, &response)
	if err != nil {
		renderHTTPError(log, r, w, errors.Wrap(err, "failed to unmarshal body"), http.StatusInternalServerError)
		return
	}

	log.WithFields(logrus.Fields{
		"assistant.status":  res.StatusCode,
		"assistant.outcome": response.Details.Outcome,
	}).Info("assistant responded")

	// The UI only reads "message"; trace_id/outcome let callers (load generator,
	// support) jump straight to the agent trace.
	json.NewEncoder(w).Encode(Response{Message: response.Content, TraceID: response.TraceID, Outcome: response.Details.Outcome, Checkout: response.Checkout, CompleteSetup: response.Checkout != nil && response.Checkout.CompleteSetup})
}

func (fe *frontendServer) setCurrencyHandler(w http.ResponseWriter, r *http.Request) {
	log := r.Context().Value(ctxKeyLog{}).(logrus.FieldLogger)
	cur := r.FormValue("currency_code")
	payload := validator.SetCurrencyPayload{Currency: cur}
	if err := payload.Validate(); err != nil {
		renderHTTPError(log, r, w, validator.ValidationErrorResponse(err), http.StatusUnprocessableEntity)
		return
	}
	log.WithField("curr.new", payload.Currency).WithField("curr.old", currentCurrency(r)).
		Debug("setting currency")

	if payload.Currency != "" {
		http.SetCookie(w, &http.Cookie{
			Name:   cookieCurrency,
			Value:  payload.Currency,
			MaxAge: cookieMaxAge,
		})
	}
	referer := r.Header.Get("referer")
	if referer == "" {
		referer = baseUrl + "/"
	}
	w.Header().Set("Location", referer)
	w.WriteHeader(http.StatusFound)
}

// chooseAd queries for advertisements available and randomly chooses one, if
// available. It ignores the error retrieving the ad since it is not critical.
func (fe *frontendServer) chooseAd(ctx context.Context, ctxKeys []string, log logrus.FieldLogger) *pb.Ad {
	ads, err := fe.getAd(ctx, ctxKeys)
	if err != nil {
		log.WithField("error", err).Warn("failed to retrieve ads")
		return nil
	}
	return ads[rand.Intn(len(ads))]
}

func renderHTTPError(log logrus.FieldLogger, r *http.Request, w http.ResponseWriter, err error, code int) {
	log.WithField("error", err).Error("request error")
	errMsg := fmt.Sprintf("%+v", err)

	w.WriteHeader(code)

	if templateErr := templates.ExecuteTemplate(w, "error", injectCommonTemplateData(r, map[string]interface{}{
		"error":       errMsg,
		"status_code": code,
		"status":      http.StatusText(code),
	})); templateErr != nil {
		log.Println(templateErr)
	}
}

func injectCommonTemplateData(r *http.Request, payload map[string]interface{}) map[string]interface{} {
	data := map[string]interface{}{
		"session_id":        sessionID(r),
		"request_id":        r.Context().Value(ctxKeyRequestID{}),
		"user_currency":     currentCurrency(r),
		"platform_css":      plat.css,
		"platform_name":     plat.provider,
		"is_cymbal_brand":   isCymbalBrand,
		"assistant_enabled": assistantEnabled,
		"deploymentDetails": deploymentDetailsMap,
		"frontendMessage":   frontendMessage,
		"currentYear":       time.Now().Year(),
		"baseUrl":           baseUrl,
	}
	if sess := accountFromContext(r.Context()); sess.Authenticated {
		data["account_logged_in"] = true
		data["account_first_name"] = sess.FirstName
		data["account_profile_incomplete"] = !sess.Unknown && !sess.ProfileComplete
	}
	data["account_enabled"] = accountDashboardEnabled

	for k, v := range payload {
		data[k] = v
	}

	return data
}

func currentCurrency(r *http.Request) string {
	c, _ := r.Cookie(cookieCurrency)
	if c != nil {
		return c.Value
	}
	return defaultCurrency
}

func sessionID(r *http.Request) string {
	v := r.Context().Value(ctxKeySessionID{})
	if v != nil {
		return v.(string)
	}
	return ""
}

func cartIDs(c []*pb.CartItem) []string {
	out := make([]string, len(c))
	for i, v := range c {
		out[i] = v.GetProductId()
	}
	return out
}

// get total # of items in cart
func cartSize(c []*pb.CartItem) int {
	cartSize := 0
	for _, item := range c {
		cartSize += int(item.GetQuantity())
	}
	return cartSize
}

func renderMoney(money pb.Money) string {
	currencyLogo := renderCurrencyLogo(money.GetCurrencyCode())
	return fmt.Sprintf("%s%d.%02d", currencyLogo, money.GetUnits(), money.GetNanos()/10000000)
}

func renderCurrencyLogo(currencyCode string) string {
	logos := map[string]string{
		"USD": "$",
		"CAD": "$",
		"JPY": "¥",
		"EUR": "€",
		"TRY": "₺",
		"GBP": "£",
	}

	logo := "$" //default
	if val, ok := logos[currencyCode]; ok {
		logo = val
	}
	return logo
}

func stringinSlice(slice []string, val string) bool {
	for _, item := range slice {
		if item == val {
			return true
		}
	}
	return false
}
