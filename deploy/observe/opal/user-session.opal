// ============================================================================
// User Session  --  derived Interval dataset  (v2)
// ============================================================================
//
// Adapted from the Astronomy demo's original User Session definition, keeping
// its structure (make_event -> window broadcast -> make_session -> rename_col
// -> pick_col -> interface "log") and re-pointing it at Online Boutique.
//
// A session is the set of contiguous transactions a user performs, with a new
// session starting after 5 minutes of inactivity.
//
// ---------------------------------------------------------------------------
// INPUTS
// ---------------------------------------------------------------------------
//   PRIMARY    Tracing/Span                      43218800  (referenced as "@")
//   @Trace     Tracing/Trace                     43218809  <- name it "Trace"
//   @Customer  Reference Tables/Customer Records  43268370 <- name it "Customer"
//
// Add the two secondary inputs via the "..." menu on the QUERY row ->
// "Link to other dataset". They cannot be declared inline in OPAL.
// "Create new dataset" (publish) is in that same menu.
//
// ---------------------------------------------------------------------------
// WHAT CHANGED FROM THE ASTRONOMY ORIGINAL, AND WHY
// ---------------------------------------------------------------------------
//
// 1. session_id needs no case() fallback.
//    The original dug the id out of attributes['db.statement'] with a regex
//    when app.user.id was absent. Here app.session.id is always present on
//    entry-point spans, so a single extract is enough.
//
// 2. Span names carry the "hipstershop." prefix, not "oteldemo.":
//      oteldemo.CheckoutService/PlaceOrder   -> hipstershop.CheckoutService/PlaceOrder
//      grpc.oteldemo.PaymentService/Charge   -> grpc.hipstershop.PaymentService/Charge
//      oteldemo.CartService/EmptyCart        -> hipstershop.CartService/EmptyCart
//      service_name "checkout"               -> "checkoutservice"
//    service_namespace is "online-boutique" here and is NOT used as a filter.
//
// 3. Every span predicate is scoped by service_name. REQUIRED, not cosmetic:
//    hipstershop.CheckoutService/PlaceOrder appears TWICE per order -- once as
//    checkoutservice's entry-point span and once as frontend's outbound
//    "Remote call". Matching on span_name alone double-counts every order
//    (measured 436 vs the true 217). The original gets away without this only
//    because oteldemo's span names don't collide across services.
//
// 4. order_amount is broadcast across the trace. THIS IS THE SUBTLE ONE.
//    The original reads app.payment.amount off the Charge span. This app's
//    paymentservice stamps NO app.* attributes at all -- neither
//    app.customer.id nor app.session.id nor any amount. Porting the original
//    directly leaves failed_payment_amount permanently NULL while
//    payment_failure still flips true, so a payment outage shows a failure with
//    no value attached. Verified: charge_with_amount was 0 on every session
//    before the broadcast, and matches the charge count after.
//
// 5. Customer is joined on customer_id -> Customer Records' primary key (uuid),
//    not on email. The original joined on email because that was the only
//    customer identifier in oteldemo telemetry. app.customer.id here is the
//    actual PK of the roster, so it is the stronger key. email/tier/LTV then
//    come from the roster rather than from telemetry.
//
// 6. placed_order_amount is currency-safe. checkoutservice normalizes to USD
//    before charging (verified 1047/1047 PlaceOrder spans app.order.currency =
//    USD), so summing it is valid. Do NOT extend this to frontend browse spans,
//    which do carry mixed USD/EUR/JPY.
//
// ---------------------------------------------------------------------------
// TECHNIQUE NOTES
// ---------------------------------------------------------------------------
// * window(any_not_null(x), frame(ahead:30s, back:30s), group_by(trace_id))
//   is the idiomatic way to spread an attribute over every span in a trace.
//   It replaces a subquery + leftjoin and accelerates better.
//   session_id MUST be broadcast this way because it is the group_by key --
//   W3C baggage is stamped server-side, so only entry-point spans carry it.
//
// * rename_col start_time:@."Valid From" must come BEFORE pick_col. pick_col
//   after make_session refuses to run unless the temporal columns are retained,
//   and the rename is what makes them pickable by those names.
//
// * options(expiry:5m) is a deprecated alias for session_gap_exact:5m, i.e.
//   the precise-but-slower variant. Use session_gap:5m if you want speed over
//   exact session boundaries.
//
// * make_session's group_by(session_id) establishes session_id as the primary
//   key. set_primary_key is added explicitly anyway so downstream set_link
//   targets are guaranteed to resolve.
// ---------------------------------------------------------------------------

make_event

// Promote the nested app.* attributes to real typed columns.
| make_col
    session_id:string(attributes["app.session.id"]),
    customer_id:string(attributes["app.customer.id"]),
    order_amount:float64(attributes["app.order.total_value"]),
    order_currency:string(attributes["app.order.currency"])

// Spread the trace-scoped values onto every span in the trace.
| make_col
    session_id:window(any_not_null(session_id), frame(ahead:30s, back:30s), group_by(trace_id)),
    customer_id:window(any_not_null(customer_id), frame(ahead:30s, back:30s), group_by(trace_id)),
    order_amount:window(any_not_null(order_amount), frame(ahead:30s, back:30s), group_by(trace_id))

// Remember which trace was the actual checkout, for one-click pivoting.
| make_col
    checkout_trace_id:if(service_name = "checkoutservice"
      and span_name = "hipstershop.CheckoutService/PlaceOrder"
      and span_type = "Service entry point", trace_id, string_null())

| lookup on(trace_id = @Trace.trace_id), trace_error_flag:@Trace.error

| filter not is_null(session_id)

| make_session options(expiry:5m),
    placed_orders:sum(if(service_name = "checkoutservice"
      and span_name = "hipstershop.CheckoutService/PlaceOrder"
      and response_status != "Error", 1, 0)),
    placed_order_amount:sum(if(service_name = "checkoutservice"
      and span_name = "hipstershop.CheckoutService/PlaceOrder"
      and response_status != "Error", order_amount, float64_null())),
    checkout_failures:sum(if(service_name = "checkoutservice"
      and (span_name = "hipstershop.CheckoutService/PlaceOrder"
           or span_name = "hipstershop.CartService/EmptyCart")
      and response_status = "Error", 1, 0)),
    payment_failure:any_not_null(if(service_name = "paymentservice"
      and span_name = "grpc.hipstershop.PaymentService/Charge"
      and response_status = "Error", bool(1), bool(0))),
    payment_failure_count:sum(if(service_name = "paymentservice"
      and span_name = "grpc.hipstershop.PaymentService/Charge"
      and response_status = "Error", 1, 0)),
    failed_payment_amount:sum(if(service_name = "paymentservice"
      and span_name = "grpc.hipstershop.PaymentService/Charge"
      and response_status = "Error", order_amount, float64_null())),
    checkout_trace_id:any_not_null(checkout_trace_id),
    service_version:any_not_null(service_version),
    customer_id:any_not_null(customer_id),
    currency:any_not_null(order_currency),
    duration:max(end_time) - min(start_time),
    avg_latency:avg(if(service_name = "frontend", duration, duration_null())),
    num_spans:count(1),
    num_traces:count_distinct(trace_id),
    trace_list:array_agg_distinct(trace_id),
    services:array_agg_distinct(service_name),
    trace_error:(sum(if(error, 1, 0)) > 0),
    trace_error_count:sum(if(error, 1, 0)),
    group_by(session_id)

| set_label session_id

// Must precede pick_col -- see TECHNIQUE NOTES.
| rename_col start_time:@."Valid From", end_time:@."Valid To"

// Enrich from the customer roster via its primary key.
| lookup on(customer_id = @Customer.uuid),
    User:@Customer.name,
    email:@Customer.email,
    Tier:@Customer.customer_tier,
    LTV:@Customer.lifetime_value_usd

| pick_col
    start_time,
    end_time,
    User,
    email,
    Tier,
    LTV,
    duration,
    service_version,
    avg_frontend_latency:avg_latency,
    placed_orders,
    placed_order_amount,
    currency,
    checkout_trace_id,
    checkout_failures,
    payment_failure,
    payment_failure_count,
    failed_payment_amount,
    num_spans,
    num_traces,
    trace_list,
    services,
    trace_error,
    trace_error_count,
    customer_id,
    session_id

| set_primary_key session_id
| interface "log"

// ---------------------------------------------------------------------------
// AFTER PUBLISHING
// ---------------------------------------------------------------------------
// Properties -> Keys        : session_id
// Properties -> Correlation tags, add as Local (requires Observe ADMIN):
//     user.session   -> session_id
//     trace.id       -> checkout_trace_id
//     customer.tier  -> Tier
//   service.version -> service_version should appear on its own with
//   Origin = inherited, since the column passed through unchanged.
//
// Downstream link, added to the AppLogs (43255756) definition after adding
// User Session as an input there:
//     set_link ^"User Session", user_id:@"User Session".session_id
// Direction is <source>:@<Target>.<target>, and the target columns must be the
// target's primary key -- which is why User Session has to exist, and have its
// key set, before this can compile.
