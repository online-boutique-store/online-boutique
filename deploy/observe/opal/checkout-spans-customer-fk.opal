// ============================================================================
// Checkout Spans  --  derived Interval dataset with a Customer foreign key
// ============================================================================
//
// Span-level data for every span in a trace that has a customer attached,
// carrying a real customer_id column declared as a foreign key to
// Reference Tables/Customer Records. This is what makes customer <-> span
// navigable in the Context Graph rather than only at query time.
//
// Worksheet inputs:
//   PRIMARY   Tracing/Span                      43218800  (referenced as "@")
//   @customer Reference Tables/Customer Records  43268370  <- name it "customer"
//
// Add @customer via the "..." menu on the query row -> "Link to other dataset",
// or the Input menu in the OPAL console.
//
// ---------------------------------------------------------------------------
// WHY YOU CANNOT PUT THIS LINK ON Tracing/Span ITSELF
// ---------------------------------------------------------------------------
// Tracing/Span is an app-managed ingest dataset. Its definition is owned by the
// Tracing app, so you cannot add a make_col or a set_link to it, and any such
// edit would be overwritten on app upgrade. A foreign key from span data to
// Customer Records therefore has to live on a NEW derived dataset. That is a
// general Observe rule, not a quirk of this app.
//
// ---------------------------------------------------------------------------
// WHY THE SUBQUERY / BROADCAST
// ---------------------------------------------------------------------------
// app.customer.id is stamped server-side via W3C baggage, so it lands only on
// the checkout fan-out subtree. Measured over 1 hour:
//
//   productcatalogservice GetProduct              867 spans
//   checkoutservice       PlaceOrder              352
//   cartservice           GetCart / EmptyCart     352 each
//   emailservice          SendOrderConfirmation   352
//   shippingservice       GetQuote / ShipOrder    352 each
//   paymentservice        (anything)                0   <-- gap
//   frontend              (anything)                0   <-- gap
//
// paymentservice stamps NEITHER app.customer.id NOR app.session.id. Filtering
// directly on the attribute would therefore silently exclude every payment span
// -- exactly the spans you need when demoing checkout/payment failures.
//
// Re-keying by trace_id and broadcasting fixes it. Verified: all 9 services
// present afterwards, paymentservice 356 spans, frontend 3204.
//
// NOTE: set_link also accepts the nested path directly, so the extraction is
// optional:
//     set_link ^Customer, attributes."app.customer.id":@customer.uuid
// A real column is preferred -- it is filterable, groupable and visible in the
// column list, whereas a JSON path is none of those.
// ---------------------------------------------------------------------------

@cust <- @ {
  make_col cid:string(attributes."app.customer.id")
  filter not is_null(cid)
  statsby cid:any_not_null(cid), group_by(trace_id)
}

// Drop infrastructure chatter.
filter span_name != "opentelemetry.proto.collector.trace.v1.TraceService/Export"
  and span_name != "grpc.health.v1.Health/Check"
  and span_name != "grpc.grpc.health.v1.Health/Check"

// Broadcast the customer onto every span in the trace, then keep only
// customer-bearing traces.
| leftjoin on(trace_id = @cust.trace_id), customer_id:@cust.cid
| filter not is_null(customer_id)

// Promote the useful app.* attributes to real columns.
| make_col
    session_id:string(attributes."app.session.id"),
    order_id:string(attributes."app.order.id"),
    order_value:float64(attributes."app.order.total_value"),
    order_currency:string(attributes."app.order.currency"),
    rpc_method:string(attributes."rpc.method")

// The foreign key. customer_id must match Customer Records' primary key
// (uuid) exactly -- set_link requires a PK or declared candidate key on the
// target, and the two column types must be comparable (both String here).
| set_link ^Customer, customer_id:@customer.uuid
