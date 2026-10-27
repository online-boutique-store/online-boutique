# Account experience: telemetry contract

What the account experience (accountservice, dashboard, durable checkout, payment methods, assistant
order tools, `CustomerUser` load) exports to Observe, and what it guarantees **not** to export. All of it
is verified locally by `bash src/accountservice/tests/run_all.sh` against a local OTLP receiver; nothing
here has been verified in Observe yet (see the runbook for the post-deploy checks).

## 1. What is never exported

Names, email addresses (or hashes of them), phone numbers, street addresses, passwords and password
hashes, activation codes, cookies, **raw browser session ids**, card numbers (PAN), CVV, and payment
tokens. Enforcement is layered, and each layer has a negative control that proves the canary scans can
fail:

| Layer | Where | Behaviour |
|---|---|---|
| Typed schema allowlist | `telemetry_schema.py` (Python), `telemetry_account.go` / `telemetry_guard.go` (Go), `otel.js` `APP_SCHEMA` (Node) | an `app.*` attribute that is not in the schema, or has the wrong type/enum/pattern, is **dropped** (fail closed) and counted in `telemetry.invalid_attr` |
| Value scrubbing | each service's sanitizing exporter | emails, phone numbers, card-like digit runs and `tok_demo_*` tokens in any string attribute, span name, event, link or status text are replaced |
| Registered-value scrubbing | `telemetry_sanitize.register_*` | the actual street/city/name/email values of the request being served are registered and redacted wherever they appear (e.g. inside an exception message) |
| Baggage allowlist | Python/Go/Node propagators | only `app.customer.id`, `app.customer.authenticated`, `app.order.id`, `app.session.ref`, `app.order.channel`, `app.prompt.version` (plus legacy non-PII request keys) cross a service boundary, inbound and outbound |
| Drop lists | exporters | `http.url`, query strings, request/response headers, `db.query.parameter`, `rpc.*.metadata`, exception messages/stacks |
| No request logging | paymentservice, emailservice, checkoutservice | card data and recipients are never logged |

Test-only switches (`TELEMETRY_TEST_MODE=1` + `TELEMETRY_SANITIZER_DISABLED` / `TELEMETRY_DISABLE_LAYERS`)
exist so tests can show the canary checks go red without a layer. They are ignored unless
`TELEMETRY_TEST_MODE=1`, which must never be set in the cluster.

## 2. Identity attributes

| Attribute | Value | Notes |
|---|---|---|
| `app.customer.id` | Postgres `customers.id` UUID | **signed-in customers only**; never a session id, never an email |
| `app.customer.authenticated` | bool | true/false on every span that knows |
| `app.session.ref` | 16 hex chars, `HMAC-SHA256(TELEMETRY_REF_KEY, session id)` | stable per browser session, not reversible, empty if the key is unset |
| `user.id` / `enduser.id` (assistant) | customer UUID, else `app.session.ref` | what Observe AI Agents groups conversations by |
| `gen_ai.conversation.id` / `app.conversation.id` | client-supplied id, else `app.session.ref` | no longer defaults to the raw session id |

**Behaviour change to be aware of in Observe:** the previous assistant telemetry exported
`app.session.id`, `app.customer.id` (= the raw session id), `app.customer.email`, `app.customer.name` and
`app.customer.email_hash`, and mirrored the session id into log `UserId`/`session`. Those are gone. Log
`UserId` and `session` are still set (so per-session correlation in assistant logs works) but now hold
`app.session.ref`. Any dashboard, monitor or reference-table join keyed on the old values (for example
the `customer_records` join on `app.customer.id`) will need re-keying. Cartservice's own `UserId` log field is
outside this work and is unchanged.

## 3. Attribute reference (`app.*`)

Account: `app.account.action` (enum: register, activate, login, logout, profile_update, address_*,
payment_*, orders_list, order_view, dashboard_view, claim_guest_orders, validate, session_resolve,
assistant_signup, profile_get, address_list, payment_list, payment_resolve; the frontend also uses login_redirect),
`app.account.outcome`, `.address_count`, `.payment_method_count`, `.profile_version`, `.created_via`,
`.service_caller`, `.guest_claim`, `.guest_orders_claimed`, `.db.outcome`, `.migration.version`,
`.migration.outcome`, `.schema.validation`, `.handoff.outcome`, `.activation.outcome`.
Validation: `app.validation.fields`, `app.validation.codes` (field names and stable codes only, never values).

Order / checkout: `app.order.id` (uuid5 of the idempotency key), `.channel` (web|assistant), `.currency`,
`.item_count`, `.total_minor_units`, `.status`, `.persistence_status` (recorded, history_unrecorded,
pending, dead), `.outbox_status`, `.record_phase` (intent|completed), `.retry_count`,
`.persistence_latency_ms`; `app.checkout.idempotency_status` (new, replayed, in_progress, resumed,
conflict, rejected), `.state` (STARTED … COMPLETED), `.resume_from`, `.lease_epoch`.

Payment / shipping: `app.payment.method_type` (demo_card_saved|card_form), `.provider` (demo-psp),
`.status` (tokenized, charged, declined, uncertain, failed), `app.shipping.status`, `.is_po_box`.

## 4. Spans per workflow (the coverage matrix)

`src/accountservice/tests/telemetry_matrix.yaml` is the authoritative list; `test_accountservice`,
`test_payment_methods`, `test_checkout_durability` and `test_frontend_e2e` fail if a row's server
attributes or child spans are missing. Rows: **R01-R25** one per `AccountService` RPC (a test fails if an
RPC has no row), **F01-F14** one per frontend account route, **X01-X04** cross-service workflows.

Key traces:

* **Checkout** (one trace across frontend → checkout → payment → shipping → email): `checkout.idem.begin`
  (new/replayed/resumed/conflict) → `checkout.plan` → `orders.publish.intent` → `checkout.payment` →
  `checkout.shipping` → `checkout.finalize` → `orders.publish.completed`. Redis spans (`redis checkout.*`,
  `charge.record`, `shipment.record`) are CLIENT spans with `db.system`, `server.address`, `peer.service`.
* **Order recording** (separate trace): `orders.consume` (CONSUMER) is **linked to**, not a child of,
  `orders.publish.*`, then `orders.persist` → `accounts.db.order.apply` → psycopg spans. The link carries the
  publish span; Observe shows it as an async hop.
* **Resume**: `checkout.janitor.resume` carries `app.checkout.resume_from` and `app.checkout.lease_epoch`.
* **Tokenization**: `AddPaymentMethod` (accountservice) → `PaymentService/Tokenize` (paymentservice) →
  `redis token.bind`; status `tokenized` or `failed` (`card_not_allowed_demo`).
* The exporters for checkout and shipping use an **uninstrumented** collector connection (otherwise every
  OTLP export created more spans).

## 5. Metrics

`app.account.actions` (action, outcome), `app.account.rpc.duration`, `app.account.login` (outcome),
`app.account.validation_failures` (field, code), `app.account.db.duration` (operation, outcome),
`app.account.activation.events`, `app.account.pending_reaped`, `app.orders.recorded` (path),
`app.orders.outbox.dead_total`, `app.orders.outbox.retries`, `app.orders.persistence.latency` (ms),
`app.payment.tokenize` (outcome: tokenized, rejected, unavailable), `telemetry.invalid_attr`.
Metric label sets are bounded enums (no ids, no user input).

## 6. Known limitations

* `app.orders.outbox.depth` / oldest-age are exposed through `GetOrderQueueStatus` and the history page's "may
  take a moment" note, not yet as exported gauges.
* Order history is eventually consistent by design: a completed checkout whose publish fails reports
  `record_status=history_unrecorded` to the shopper and is rebuilt by the reconciler/janitor from the checkout
  state; it is never silently lost, but it can appear in history late.
* The OTLP receiver used locally is not Observe. Attribute names, links, and the AI Agents view have not been
  re-verified against the Observe tenant for this change.
