package main

import (
	"context"
	"crypto/sha256"
	"encoding/base64"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"regexp"
	"strings"
	"sync"
	"time"

	"github.com/golang/protobuf/proto"
	"github.com/google/uuid"
	"github.com/redis/go-redis/v9"
	"go.opentelemetry.io/otel"
	"go.opentelemetry.io/otel/attribute"
	otelcodes "go.opentelemetry.io/otel/codes"
	"go.opentelemetry.io/otel/trace"
	"google.golang.org/grpc/codes"
	"google.golang.org/grpc/metadata"
	"google.golang.org/grpc/status"

	pb "github.com/GoogleCloudPlatform/microservices-demo/src/checkoutservice/genproto"
	money "github.com/GoogleCloudPlatform/microservices-demo/src/checkoutservice/money"
)

// Durable, restart-safe checkout.
//
//   STARTED -> PAYMENT_ATTEMPTING -> PAYMENT_CHARGED -> SHIPPING_ATTEMPTING -> SHIPPED -> COMPLETED
//                  |  \-> PAYMENT_UNCERTAIN -/                                   (stream: intent, completed)
//                  \-> PAYMENT_DECLINED (terminal)
//
// * The idempotency key is stable per checkout attempt; the order id derives from it (UUIDv5).
// * Entering PAYMENT_ATTEMPTING atomically publishes the order INTENT; if that fails nothing is charged.
// * Payment and shipping are idempotent on the same key (GetCharge / GetShipment resolve unknowns).
// * Entering COMPLETED atomically publishes the COMPLETED order; if only that step fails after the
//   customer was charged and shipped, the response is success with record_status=history_unrecorded and
//   the completion is retried in the background (and by the janitor) until the queue returns.
// * A retry with the same key replays the stored result, resumes the saved state, or reports in_progress.

var (
	orderNamespace = uuid.MustParse("6f1a2b3c-4d5e-4f60-8a7b-9c0d1e2f3a4b")
	reKey          = regexp.MustCompile(`^[A-Za-z0-9._:-]{8,80}$`)
	checkoutTracer = otel.Tracer("checkoutservice")
)

type moneyJSON struct {
	C string `json:"c"`
	U int64  `json:"u"`
	N int32  `json:"n"`
}

func toMoneyJSON(m *pb.Money) moneyJSON {
	return moneyJSON{C: m.GetCurrencyCode(), U: m.GetUnits(), N: m.GetNanos()}
}
func (m moneyJSON) pb() *pb.Money { return &pb.Money{CurrencyCode: m.C, Units: m.U, Nanos: m.N} }

type planItem struct {
	ProductID string    `json:"product_id"`
	Name      string    `json:"name"`
	Category  string    `json:"category"`
	Quantity  int32     `json:"quantity"`
	Unit      moneyJSON `json:"unit"`
}

type planAddress struct {
	Line1   string `json:"line1"`
	Line2   string `json:"line2"`
	City    string `json:"city"`
	State   string `json:"state"`
	Zip     string `json:"zip"`
	Country string `json:"country"`
}

// plan is everything needed to finish a checkout after a crash, WITHOUT the card number.
type plan struct {
	UserID          string      `json:"user_id"`
	Email           string      `json:"email"`
	Currency        string      `json:"currency"`
	Channel         string      `json:"channel"`
	CustomerID      string      `json:"customer_id"`
	SessionRef      string      `json:"session_ref"`
	PaymentMethodID string      `json:"payment_method_id"`
	PaymentType     string      `json:"payment_type"`
	Brand           string      `json:"brand"`
	Last4           string      `json:"last4"`
	Address         planAddress `json:"address"`
	Items           []planItem  `json:"items"`
	Subtotal        moneyJSON   `json:"subtotal"`
	Shipping        moneyJSON   `json:"shipping"`
	Total           moneyJSON   `json:"total"`
	IntentMs        int64       `json:"intent_ms"`
	PromptVersion   string      `json:"prompt_version"`
}

type orderMsg struct {
	V            int           `json:"v"`
	Phase        string        `json:"phase"`
	OrderID      string        `json:"order_id"`
	Key          string        `json:"key"`
	CustomerID   *string       `json:"customer_id"`
	SessionRef   string        `json:"session_ref"`
	EmailHash    string        `json:"email_hash"`
	EmailDisplay string        `json:"email_display,omitempty"`
	Channel      string        `json:"channel"`
	Currency     string        `json:"currency"`
	IntentMs     int64         `json:"intent_ms"`
	PlacedMs     int64         `json:"placed_ms,omitempty"`
	Items        []planItem    `json:"items"`
	Subtotal     moneyJSON     `json:"subtotal"`
	Shipping     moneyJSON     `json:"shipping"`
	Total        moneyJSON     `json:"total"`
	TrackingID   string        `json:"tracking_id,omitempty"`
	TxnID        string        `json:"txn_id,omitempty"`
	Payment      orderPayment  `json:"payment"`
	Ship         orderShipping `json:"ship"`
	FailReason   string        `json:"fail_reason,omitempty"`
}

type orderPayment struct {
	MethodID string `json:"method_id"`
	Type     string `json:"type"`
	Brand    string `json:"brand"`
	Last4    string `json:"last4"`
}
type orderShipping struct {
	Line1   string `json:"line1"`
	Line2   string `json:"line2"`
	City    string `json:"city"`
	State   string `json:"state"`
	Zip     string `json:"zip"`
	Country string `json:"country"`
	IsPOBox bool   `json:"is_po_box"`
}

var rePOBox = regexp.MustCompile(`(?i)\b(?:P\.?\s?O\.?\s*Box|Post\s+Office\s+Box)\b`)

func (p *plan) message(phase, key, orderID, txn, tracking, failReason string, placedMs int64) string {
	m := orderMsg{V: 1, Phase: phase, OrderID: orderID, Key: key, SessionRef: p.SessionRef,
		EmailHash: emailHash(p.Email), Channel: p.Channel, Currency: p.Currency, IntentMs: p.IntentMs, PlacedMs: placedMs,
		Items: p.Items, Subtotal: p.Subtotal, Shipping: p.Shipping, Total: p.Total, TrackingID: tracking, TxnID: txn,
		Payment:    orderPayment{MethodID: p.PaymentMethodID, Type: p.PaymentType, Brand: p.Brand, Last4: p.Last4},
		Ship:       orderShipping{Line1: p.Address.Line1, Line2: p.Address.Line2, City: p.Address.City, State: p.Address.State, Zip: p.Address.Zip, Country: p.Address.Country, IsPOBox: rePOBox.MatchString(p.Address.Line1 + " " + p.Address.Line2)},
		FailReason: failReason}
	if p.CustomerID != "" {
		id := p.CustomerID
		m.CustomerID = &id
		m.EmailDisplay = p.Email
	}
	b, _ := json.Marshal(m)
	return string(b)
}

func emailHash(email string) string {
	h := sha256.Sum256([]byte(strings.ToLower(strings.TrimSpace(email))))
	return hex.EncodeToString(h[:])
}

func reasonErr(code codes.Code, reason string) error { return status.Error(code, "reason:"+reason) }

// ---- request plumbing --------------------------------------------------------------------------

func brandOf(number string) string {
	n := strings.NewReplacer(" ", "", "-", "").Replace(number)
	switch {
	case strings.HasPrefix(n, "4"):
		return "visa"
	case len(n) >= 2 && n[:2] >= "51" && n[:2] <= "55", strings.HasPrefix(n, "2"):
		return "mastercard"
	case strings.HasPrefix(n, "34"), strings.HasPrefix(n, "37"):
		return "amex"
	case strings.HasPrefix(n, "6"):
		return "discover"
	}
	return "visa"
}

func last4Of(number string) string {
	d := strings.Map(func(r rune) rune {
		if r >= '0' && r <= '9' {
			return r
		}
		return -1
	}, number)
	if len(d) < 4 {
		return "0000"
	}
	return d[len(d)-4:]
}

func postalOf(a *pb.Address) string {
	if a.GetPostalCode() != "" {
		return a.GetPostalCode()
	}
	if a.GetZipCode() == 0 {
		return ""
	}
	return fmt.Sprintf("%05d", a.GetZipCode())
}

func addressFrom(a *pb.Address) planAddress {
	return planAddress{Line1: a.GetStreetAddress(), Line2: a.GetAddressLine2(), City: a.GetCity(), State: a.GetState(), Zip: postalOf(a), Country: a.GetCountry()}
}

// resolveCustomer derives the customer from the caller's account session (never from the request body).
// "" = guest. If accountservice is unreachable the order is recorded as a guest order for this browser
// session; ClaimGuestOrders attaches it to the customer later.
func (cs *checkoutService) resolveCustomer(ctx context.Context) string {
	if cs.account == nil {
		return ""
	}
	md, _ := metadata.FromIncomingContext(ctx)
	tok := md.Get("x-account-session")
	if len(tok) == 0 || tok[0] == "" {
		return ""
	}
	ctx, span := checkoutTracer.Start(ctx, "checkout.resolve_customer")
	defer span.End()
	id, err := cs.account.resolve(ctx, tok[0])
	if err != nil {
		span.SetAttributes(attribute.String("error.type", "account_unavailable"))
		return ""
	}
	return id
}

// ---- PlaceOrder ----------------------------------------------------------------------------------

func (cs *checkoutService) PlaceOrder(ctx context.Context, req *pb.PlaceOrderRequest) (*pb.PlaceOrderResponse, error) {
	if cs.queue == nil {
		return cs.placeOrderLegacy(ctx, req)
	}
	stampAppAttrs(ctx)
	key := req.GetIdempotencyKey()
	if key == "" {
		key = "auto-" + uuid.NewString()
	}
	if !reKey.MatchString(key) {
		return nil, reasonErr(codes.InvalidArgument, "invalid_idempotency_key")
	}
	orderID := uuid.NewSHA1(orderNamespace, []byte(key)).String()
	channel := "web"
	if req.GetChannel() == "assistant" {
		channel = "assistant"
	}
	customerID := cs.resolveCustomer(ctx)
	ref := sessionRef(req.GetUserId())

	ctx = withBaggage(ctx, "app.order.id", orderID)
	ctx = withBaggage(ctx, "app.order.channel", channel)
	ctx = withBaggage(ctx, "app.session.ref", ref)
	ctx = withBaggage(ctx, "app.customer.authenticated", boolStr(customerID != ""))
	if customerID != "" {
		ctx = withBaggage(ctx, "app.customer.id", customerID)
	}
	stampAppAttrs(ctx)
	span := trace.SpanFromContext(ctx)

	cc := req.GetCreditCard()
	fp := fingerprint(req.GetUserId(), req.GetUserCurrency(), req.GetEmail(), req.GetAddress().GetStreetAddress(), req.GetAddress().GetAddressLine2(),
		req.GetAddress().GetCity(), req.GetAddress().GetState(), req.GetAddress().GetCountry(), postalOf(req.GetAddress()), channel,
		req.GetPaymentMethodId(), cc.GetCreditCardNumber(), fmt.Sprint(cc.GetCreditCardExpirationYear(), cc.GetCreditCardExpirationMonth()), customerID)

	bctx, bspan := checkoutTracer.Start(ctx, "checkout.idem.begin")
	br, err := cs.queue.begin(bctx, key, fp, orderID)
	if err != nil {
		bspan.End()
		return nil, reasonErr(codes.Unavailable, "order_queue_unavailable")
	}
	setAppAttrs(bspan, attribute.String("app.checkout.state", br.State), attribute.Int("app.checkout.lease_epoch", int(br.Epoch)))
	bspan.End()
	idem := map[string]string{"NEW": "new", "RESUME": "resumed", "REPLAY": "replayed", "IN_PROGRESS": "in_progress", "CONFLICT": "conflict", "FAILED": "replayed"}[br.Kind]
	setAppAttrs(span, attribute.String("app.checkout.idempotency_status", idem), attribute.String("app.checkout.state", br.State),
		attribute.Int("app.checkout.lease_epoch", int(br.Epoch)))

	switch br.Kind {
	case "CONFLICT":
		return nil, reasonErr(codes.FailedPrecondition, "idempotency_conflict")
	case "IN_PROGRESS":
		return nil, reasonErr(codes.Aborted, "checkout_in_progress")
	case "FAILED":
		return nil, reasonErr(codes.FailedPrecondition, firstNonEmpty(br.Extra, "payment_declined"))
	case "REPLAY":
		return cs.replay(ctx, key, br.Extra)
	}
	if br.Kind == "RESUME" {
		setAppAttrs(span, attribute.String("app.checkout.resume_from", br.State))
	}

	r := &run{cs: cs, key: key, epoch: br.Epoch, orderID: orderID, state: br.State, req: req, resumed: br.Kind == "RESUME"}
	var p *plan
	if br.State == stStarted {
		pctx, pspan := checkoutTracer.Start(ctx, "checkout.plan")
		p, err = cs.buildPlan(pctx, req, customerID, ref, channel)
		if err != nil {
			pspan.RecordError(err)
			pspan.SetStatus(otelcodes.Error, "plan failed")
			pspan.End()
			cs.queue.release(ctx, key, br.Epoch)
			return nil, err
		}
		pspan.End()
		p.IntentMs = time.Now().UnixMilli()
		pj, _ := json.Marshal(p)
		ictx, ispan := checkoutTracer.Start(ctx, "orders.publish.intent")
		_, err = cs.queue.advance(ictx, key, br.Epoch, []string{stStarted}, stPayAttempt, p.message("intent", key, orderID, "", "", "", 0), "intent", orderID, "plan", string(pj))
		if err != nil {
			ispan.RecordError(err)
			ispan.SetStatus(otelcodes.Error, "publish failed")
			ispan.End()
			// Nothing has been charged. Fail closed.
			if err == errStale {
				return nil, reasonErr(codes.Aborted, "checkout_in_progress")
			}
			return nil, reasonErr(codes.Unavailable, "order_queue_unavailable")
		}
		setAppAttrs(ispan, attribute.String("app.order.record_phase", "intent"), attribute.String("app.order.outbox_status", "accepted"))
		ispan.End()
		r.state = stPayAttempt
		crashAt("after_intent")
	} else {
		f, ferr := cs.queue.fields(ctx, key, "plan")
		if ferr != nil || f["plan"] == "" {
			cs.queue.release(ctx, key, br.Epoch)
			return nil, reasonErr(codes.Unavailable, "order_queue_unavailable")
		}
		p = &plan{}
		if json.Unmarshal([]byte(f["plan"]), p) != nil {
			return nil, reasonErr(codes.Internal, "corrupt_checkout_state")
		}
	}
	r.plan = p
	stampOrderAttrs(ctx, *p.Total.pb(), itemDescriptions(p))
	setAppAttrs(span, attribute.String("app.order.currency", p.Currency), attribute.Int("app.order.item_count", len(p.Items)))
	return r.drive(ctx)
}

func firstNonEmpty(a, b string) string {
	if a != "" {
		return a
	}
	return b
}

func boolStr(b bool) string {
	if b {
		return "true"
	}
	return "false"
}

func itemDescriptions(p *plan) []string {
	out := make([]string, len(p.Items))
	for i, it := range p.Items {
		out[i] = fmt.Sprintf("%s x%d @ %.2f %s", it.Name, it.Quantity, float64(it.Unit.U)+float64(it.Unit.N)/1e9, p.Currency)
	}
	return out
}

func (cs *checkoutService) replay(ctx context.Context, key, resp string) (*pb.PlaceOrderResponse, error) {
	raw, err := base64.StdEncoding.DecodeString(resp)
	if err != nil {
		return nil, reasonErr(codes.Internal, "corrupt_checkout_state")
	}
	var order pb.OrderResult
	if err := proto.Unmarshal(raw, &order); err != nil {
		return nil, reasonErr(codes.Internal, "corrupt_checkout_state")
	}
	rec := "recorded"
	if f, err := cs.queue.fields(ctx, key, "pub_completed"); err == nil && f["pub_completed"] == "" {
		rec = "history_unrecorded"
	}
	return &pb.PlaceOrderResponse{Order: &order, Status: "completed", RecordStatus: rec, Replayed: true}, nil
}

// buildPlan snapshots the cart, prices, shipping quote and payment display data. Pure reads: safe to redo.
func (cs *checkoutService) buildPlan(ctx context.Context, req *pb.PlaceOrderRequest, customerID, ref, channel string) (*plan, error) {
	cartItems, err := cs.getUserCart(ctx, req.GetUserId())
	if err != nil {
		return nil, reasonErr(codes.Unavailable, "cart_unavailable")
	}
	if len(cartItems) == 0 {
		return nil, reasonErr(codes.FailedPrecondition, "cart_empty")
	}
	orderItems, _, err := cs.prepOrderItems(ctx, cartItems, req.GetUserCurrency())
	if err != nil {
		return nil, reasonErr(codes.Unavailable, "catalog_unavailable")
	}
	meta, _ := cs.productMeta(ctx, cartItems)
	shippingUSD, err := cs.quoteShipping(ctx, req.GetAddress(), cartItems)
	if err != nil {
		return nil, reasonErr(codes.Unavailable, "shipping_unavailable")
	}
	shipping, err := cs.convertCurrency(ctx, shippingUSD, req.GetUserCurrency())
	if err != nil {
		return nil, reasonErr(codes.Unavailable, "currency_unavailable")
	}
	subtotal := pb.Money{CurrencyCode: req.GetUserCurrency()}
	p := &plan{UserID: req.GetUserId(), Email: req.GetEmail(), Currency: req.GetUserCurrency(), Channel: channel, CustomerID: customerID,
		SessionRef: ref, PaymentMethodID: req.GetPaymentMethodId(), Address: addressFrom(req.GetAddress()),
		PromptVersion: promptVersionFrom(ctx)}
	for _, it := range orderItems {
		pid := it.GetItem().GetProductId()
		p.Items = append(p.Items, planItem{ProductID: pid, Name: meta[pid].name, Category: meta[pid].category, Quantity: it.GetItem().GetQuantity(), Unit: toMoneyJSON(it.Cost)})
		subtotal = money.Must(money.Sum(subtotal, money.MultiplySlow(*it.Cost, uint32(it.GetItem().GetQuantity()))))
	}
	total := money.Must(money.Sum(subtotal, *shipping))
	p.Subtotal, p.Shipping, p.Total = toMoneyJSON(&subtotal), toMoneyJSON(shipping), toMoneyJSON(&total)
	if req.GetPaymentMethodId() != "" {
		// Saved methods belong to a signed-in customer; the method must resolve for THAT customer.
		p.PaymentType = "demo_card_saved"
		if cs.account == nil || customerID == "" {
			return nil, reasonErr(codes.FailedPrecondition, "login_required")
		}
		t, err := cs.account.resolveToken(ctx, customerID, req.GetPaymentMethodId())
		if err != nil {
			if status.Code(err) == codes.NotFound {
				return nil, reasonErr(codes.FailedPrecondition, "payment_method_unavailable")
			}
			return nil, reasonErr(codes.Unavailable, "account_service_unavailable")
		}
		p.Brand, p.Last4 = t.brand, t.last4
	} else {
		p.PaymentType = "card_form"
		p.Brand, p.Last4 = brandOf(req.GetCreditCard().GetCreditCardNumber()), last4Of(req.GetCreditCard().GetCreditCardNumber())
	}
	return p, nil
}

func promptVersionFrom(ctx context.Context) string { return "" }

type productInfo struct{ name, category string }

func (cs *checkoutService) productMeta(ctx context.Context, items []*pb.CartItem) (map[string]productInfo, error) {
	out := map[string]productInfo{}
	cl := pb.NewProductCatalogServiceClient(cs.productCatalogSvcConn)
	for _, it := range items {
		pr, err := cl.GetProduct(ctx, &pb.GetProductRequest{Id: it.GetProductId()})
		if err != nil {
			return out, err
		}
		cat := ""
		if len(pr.GetCategories()) > 0 {
			cat = pr.GetCategories()[0]
		}
		out[it.GetProductId()] = productInfo{name: pr.GetName(), category: cat}
	}
	return out, nil
}

// ---- the resumable run ---------------------------------------------------------------------------

type run struct {
	cs      *checkoutService
	key     string
	epoch   int64
	orderID string
	state   string
	plan    *plan
	req     *pb.PlaceOrderRequest // nil when resumed by the janitor (no card data available)
	txn     string
	track   string
	resumed bool
}

func (r *run) drive(ctx context.Context) (*pb.PlaceOrderResponse, error) {
	q := r.cs.queue
	defer q.keepLease(r.key, r.epoch)()
	// ---- payment
	if r.state == stStarted || r.state == stPayAttempt || r.state == stPayUncertain {
		if err := r.payStep(ctx); err != nil {
			q.release(ctx, r.key, r.epoch)
			return nil, err
		}
	} else {
		f, err := q.fields(ctx, r.key, "txn_id", "tracking_id")
		if err != nil {
			q.release(ctx, r.key, r.epoch)
			return nil, reasonErr(codes.Unavailable, "order_queue_unavailable")
		}
		r.txn, r.track = f["txn_id"], f["tracking_id"]
	}
	// ---- shipping
	if r.state == stPayCharged || r.state == stShipAttempt {
		if err := r.shipStep(ctx); err != nil {
			q.release(ctx, r.key, r.epoch)
			return nil, err
		}
	}
	// ---- finalize
	return r.finalize(ctx)
}

func (r *run) payStep(ctx context.Context) error {
	q := r.cs.queue
	pctx, span := checkoutTracer.Start(ctx, "checkout.payment")
	defer span.End()
	pay := r.cs.paymentClient()
	r.crash("before_charge")
	var txn string
	for attempt := 1; attempt <= 3 && txn == ""; attempt++ {
		if attempt > 1 || r.state != stPayAttempt || r.wasResumed() {
			gctx, gcancel := withTimeoutCancel(pctx, 3*time.Second)
			gc, err := pay.GetCharge(gctx, &pb.GetChargeRequest{IdempotencyKey: r.key})
			gcancel()
			if err == nil && gc.GetFound() {
				txn = gc.GetTransactionId()
				break
			}
		}
		if r.req == nil { // janitor: no card data; cannot (re)charge
			break
		}
		creq, cerr := r.chargeRequest(pctx)
		if cerr != nil {
			// Nothing was sent to the payment service: safe to fail and retry with the same key.
			setAppAttrs(span, attribute.String("app.payment.status", "failed"))
			if status.Code(cerr) == codes.NotFound {
				return reasonErr(codes.FailedPrecondition, "payment_method_unavailable")
			}
			return reasonErr(codes.Unavailable, "account_service_unavailable")
		}
		cctx, cancel := withTimeoutCancel(pctx, 6*time.Second)
		resp, err := pay.Charge(cctx, creq)
		cancel()
		if err == nil {
			txn = resp.GetTransactionId()
			r.crash("after_charge_call")
			break
		}
		if st, ok := status.FromError(err); ok && st.Code() == codes.InvalidArgument {
			return r.declined(pctx, span, st.Message())
		}
		// Unknown outcome: remember it, then ask the payment service before doing anything else.
		if r.state != stPayUncertain {
			if _, aerr := q.advance(pctx, r.key, r.epoch, []string{stPayAttempt}, stPayUncertain, "", "", r.orderID); aerr == nil {
				r.state = stPayUncertain
			}
		}
		setAppAttrs(span, attribute.String("app.payment.status", "uncertain"))
		time.Sleep(time.Duration(attempt) * 250 * time.Millisecond)
	}
	if txn == "" {
		setAppAttrs(span, attribute.String("app.payment.status", "uncertain"))
		return reasonErr(codes.Unavailable, "payment_uncertain")
	}
	if _, err := q.advance(pctx, r.key, r.epoch, []string{stPayAttempt, stPayUncertain}, stPayCharged, "", "", r.orderID, "txn_id", txn); err != nil {
		if err == errStale {
			return reasonErr(codes.Aborted, "checkout_in_progress")
		}
		// The customer IS charged; state could not be persisted. A retry (or the janitor) finds the charge via GetCharge.
		return reasonErr(codes.Unavailable, "order_queue_unavailable")
	}
	r.txn, r.state = txn, stPayCharged
	setAppAttrs(span, attribute.String("app.payment.status", "charged"), attribute.String("app.checkout.state", stPayCharged))
	r.crash("after_charged_state")
	return nil
}

func (r *run) wasResumed() bool { return r.state == stPayUncertain || r.resumed }

func (r *run) declined(ctx context.Context, span trace.Span, msg string) error {
	setAppAttrs(span, attribute.String("app.payment.status", "declined"))
	fail := r.plan.message("failed", r.key, r.orderID, "", "", "payment_declined", 0)
	if _, err := r.cs.queue.advance(ctx, r.key, r.epoch, []string{stPayAttempt, stPayUncertain}, stPayDeclined, fail, "failed", r.orderID, "fail_reason", "payment_declined"); err != nil {
		log.WithContext(ctx).Warnf("could not persist decline: %v", err)
	}
	return reasonErr(codes.FailedPrecondition, "payment_declined")
}

// chargeRequest builds the Charge call. A saved method is charged by token only (resolved just in time,
// never stored in checkout state); if the token cannot be resolved the error is returned rather than
// falling back to the card form, so a lookup outage is never mistaken for a declined card.
func (r *run) chargeRequest(ctx context.Context) (*pb.ChargeRequest, error) {
	total := r.plan.Total.pb()
	cr := &pb.ChargeRequest{Amount: total, IdempotencyKey: r.key}
	if r.req.GetPaymentMethodId() != "" {
		if r.cs.account == nil {
			return nil, fmt.Errorf("no account service")
		}
		t, err := r.cs.account.resolveToken(ctx, r.plan.CustomerID, r.req.GetPaymentMethodId())
		if err != nil {
			return nil, err
		}
		cr.PaymentToken = t.token
		return cr, nil
	}
	cr.CreditCard = r.req.GetCreditCard()
	return cr, nil
}

func (r *run) shipStep(ctx context.Context) error {
	q := r.cs.queue
	sctx, span := checkoutTracer.Start(ctx, "checkout.shipping")
	defer span.End()
	if r.state == stPayCharged {
		if _, err := q.advance(sctx, r.key, r.epoch, []string{stPayCharged}, stShipAttempt, "", "", r.orderID); err != nil {
			if err == errStale {
				return reasonErr(codes.Aborted, "checkout_in_progress")
			}
			return reasonErr(codes.Unavailable, "order_queue_unavailable")
		}
		r.state = stShipAttempt
	}
	sh := pb.NewShippingServiceClient(r.cs.shippingSvcConn)
	r.crash("before_ship")
	var track string
	gsctx, gscancel := withTimeoutCancel(sctx, 3*time.Second)
	if gs, err := sh.GetShipment(gsctx, &pb.GetShipmentRequest{IdempotencyKey: r.key}); err == nil && gs.GetFound() {
		track = gs.GetTrackingId()
	}
	gscancel()
	if track == "" {
		cctx, cancel := withTimeoutCancel(sctx, 6*time.Second)
		items := r.cartItemsFromPlan()
		resp, err := sh.ShipOrder(cctx, &pb.ShipOrderRequest{Address: r.addressPB(), Items: items, IdempotencyKey: r.key})
		cancel()
		if err != nil {
			setAppAttrs(span, attribute.String("app.shipping.status", "failed"))
			return reasonErr(codes.Unavailable, "shipping_unavailable")
		}
		track = resp.GetTrackingId()
		r.crash("after_ship_call")
	}
	if _, err := q.advance(sctx, r.key, r.epoch, []string{stShipAttempt}, stShipped, "", "", r.orderID, "tracking_id", track); err != nil {
		if err == errStale {
			return reasonErr(codes.Aborted, "checkout_in_progress")
		}
		return reasonErr(codes.Unavailable, "order_queue_unavailable")
	}
	r.track, r.state = track, stShipped
	setAppAttrs(span, attribute.String("app.shipping.status", "shipped"), attribute.String("app.checkout.state", stShipped))
	return nil
}

func (r *run) cartItemsFromPlan() []*pb.CartItem {
	out := make([]*pb.CartItem, len(r.plan.Items))
	for i, it := range r.plan.Items {
		out[i] = &pb.CartItem{ProductId: it.ProductID, Quantity: it.Quantity}
	}
	return out
}

func (r *run) addressPB() *pb.Address {
	a := r.plan.Address
	return &pb.Address{StreetAddress: a.Line1, AddressLine2: a.Line2, City: a.City, State: a.State, Country: a.Country, PostalCode: a.Zip}
}

func (r *run) orderResult() *pb.OrderResult {
	items := make([]*pb.OrderItem, len(r.plan.Items))
	for i, it := range r.plan.Items {
		items[i] = &pb.OrderItem{Item: &pb.CartItem{ProductId: it.ProductID, Quantity: it.Quantity}, Cost: it.Unit.pb()}
	}
	return &pb.OrderResult{OrderId: r.orderID, ShippingTrackingId: r.track, ShippingCost: r.plan.Shipping.pb(), ShippingAddress: r.addressPB(), Items: items}
}

func (r *run) finalize(ctx context.Context) (*pb.PlaceOrderResponse, error) {
	cs, q := r.cs, r.cs.queue
	fctx, span := checkoutTracer.Start(ctx, "checkout.finalize")
	defer span.End()
	_ = cs.emptyUserCart(fctx, r.plan.UserID) // idempotent, best effort
	result := r.orderResult()
	if f, err := q.fields(fctx, r.key, "email_sent"); err == nil && f["email_sent"] == "" && r.plan.Email != "" {
		if cs.sendOrderConfirmation(fctx, r.plan.Email, result) == nil {
			_ = q.setField(fctx, r.key, "email_sent", "1")
		} else {
			log.WithContext(fctx).Warn("order confirmation email failed")
		}
	}
	raw, _ := proto.Marshal(result)
	resp := base64.StdEncoding.EncodeToString(raw)
	r.crash("before_complete")
	placed := time.Now().UnixMilli()
	cctx, cspan := checkoutTracer.Start(fctx, "orders.publish.completed")
	_, err := q.advance(cctx, r.key, r.epoch, []string{stShipped}, stCompleted, r.plan.message("completed", r.key, r.orderID, r.txn, r.track, "", placed), "completed", r.orderID, "resp", resp)
	record := "recorded"
	if err != nil {
		cspan.RecordError(err)
		cspan.SetStatus(otelcodes.Error, "publish failed")
		if err == errStale {
			cspan.End()
			return nil, reasonErr(codes.Aborted, "checkout_in_progress")
		}
		record = "history_unrecorded"
		cs.pending.add(&pendingCompletion{key: r.key, epoch: r.epoch, orderID: r.orderID, msg: r.plan.message("completed", r.key, r.orderID, r.txn, r.track, "", placed), resp: resp})
		setAppAttrs(cspan, attribute.String("app.order.outbox_status", "unconfirmed"))
	} else {
		setAppAttrs(cspan, attribute.String("app.order.record_phase", "completed"), attribute.String("app.order.outbox_status", "accepted"))
	}
	cspan.End()
	setAppAttrs(span, attribute.String("app.order.persistence_status", map[string]string{"recorded": "recorded", "history_unrecorded": "history_unrecorded"}[record]),
		attribute.String("app.checkout.state", stCompleted))
	setAppAttrs(trace.SpanFromContext(ctx), attribute.String("app.order.persistence_status", map[string]string{"recorded": "recorded", "history_unrecorded": "history_unrecorded"}[record]))
	return &pb.PlaceOrderResponse{Order: result, Status: "completed", RecordStatus: record}, nil
}

// ---- release + pending completions + helpers ----------------------------------------------------

var releaseScript = redis.NewScript(`
if redis.call('HGET', KEYS[1], 'epoch') == ARGV[1] then redis.call('HSET', KEYS[1], 'lease_until', 0) end
return 1`)

func (q *orderQueue) release(ctx context.Context, key string, epoch int64) {
	_ = releaseScript.Run(ctx, q.rdb, []string{checkoutKeyPref + key}, fmt.Sprint(epoch)).Err()
}

type pendingCompletion struct {
	key, orderID, msg, resp string
	epoch                   int64
	tries                   int
}

type pendingSet struct {
	mu sync.Mutex
	m  map[string]*pendingCompletion
}

func (p *pendingSet) add(c *pendingCompletion) {
	p.mu.Lock()
	defer p.mu.Unlock()
	if p.m == nil {
		p.m = map[string]*pendingCompletion{}
	}
	p.m[c.key] = c
}

func (p *pendingSet) snapshot() []*pendingCompletion {
	p.mu.Lock()
	defer p.mu.Unlock()
	out := make([]*pendingCompletion, 0, len(p.m))
	for _, c := range p.m {
		out = append(out, c)
	}
	return out
}

func (p *pendingSet) done(key string) {
	p.mu.Lock()
	delete(p.m, key)
	p.mu.Unlock()
}

func withTimeoutCancel(ctx context.Context, d time.Duration) (context.Context, context.CancelFunc) {
	return context.WithTimeout(ctx, d)
}

// crash applies the test-only fault hooks for client-initiated runs only (never the janitor's).
func (r *run) crash(point string) {
	if r.req != nil {
		crashAt(point)
	}
}
