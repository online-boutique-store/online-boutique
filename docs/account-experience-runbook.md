# Account experience: runbook

Status: **implemented and tested locally; not deployed.** Nothing here has touched the EKS cluster,
Observe, or a live database. Every cluster step below is a *proposal* that needs explicit approval.

## 1. What was built

| Area | Summary |
|---|---|
| `accountservice` (new, Python gRPC) | customers, sessions, profile, addresses, saved demo payment methods, order history; Postgres is the source of truth (migrations 0001-0005, advisory-locked, checksummed) |
| Dashboard (frontend) | `/account` (+ profile, addresses, orders, order detail, payment methods); login redirects to the dashboard; logout is POST + CSRF; saved cards at checkout |
| Validation contract | one JSON contract (`account_validation.json`) shared by accountservice, frontend, assistant and loadgen; password rules unchanged |
| Durable checkout | idempotency key per submit; state machine in the `order-queue` Redis; payment and shipping are idempotent by that key; two-phase publish (`intent`, `completed`) to the `orders:v1` stream; accountservice consumer writes history |
| Synthetic payments | catalog-only tokenization in paymentservice (5 demo cards); tokens never leave the server side |
| Assistant | `list_orders`, `get_order`; saved-card checkout; idempotency key + account session on `place_order`; PII-free telemetry |
| Loadgen | `CustomerUser` journeys |

Services and ports: `accountservice` 8081 (gRPC). Callers authenticate with `x-service-name` /
`x-service-secret`; the shopper's session travels as `x-account-session`. Customer scope is always derived from
the session (never from request fields), and another customer's ids return `NOT_FOUND`.

## 2. Configuration to set (proposed; none applied)

New secret `accountservice-credentials` (`deploy/accountservice/create-secrets.sh`) and the new `order-queue`
StatefulSet (`deploy/accountservice/order-queue.yaml`: AOF `appendfsync always`, `noeviction`, 1Gi PVC).

| Service | Env to add |
|---|---|
| accountservice | `ACCOUNTS_DATABASE_URL` (existing secret), `ACCOUNT_SERVICE_SECRETS`, `TELEMETRY_REF_KEY`, `REDIS_ADDR=redis-cart:6379`, `ORDER_QUEUE_ADDR=order-queue:6379`, `PAYMENT_SERVICE_ADDR=paymentservice:50051` (all in `deploy/accountservice/accountservice.yaml`) |
| frontend | `ACCOUNT_BACKEND=grpc`, `ACCOUNT_SERVICE_ADDR=accountservice:8081`, `ACCOUNT_SERVICE_SECRET` (=`FRONTEND_SECRET`), `TELEMETRY_REF_KEY` |
| checkoutservice | `ORDER_QUEUE_ADDR`, `ACCOUNT_SERVICE_ADDR`, `ACCOUNT_SERVICE_SECRET` (=`CHECKOUT_SECRET`), `CHECKOUT_FP_KEY`, `TELEMETRY_REF_KEY` |
| paymentservice | `ORDER_QUEUE_ADDR`, `TELEMETRY_REF_KEY` |
| shippingservice | `ORDER_QUEUE_ADDR`, `TELEMETRY_REF_KEY` |
| shoppingassistantservice | `ACCOUNT_BACKEND=grpc`, `ACCOUNT_SERVICE_ADDR`, `ACCOUNT_SERVICE_SECRET` (=`ASSISTANT_SECRET`), `TELEMETRY_REF_KEY` |
| emailservice | none (code change only) |

`TELEMETRY_REF_KEY` must be the same value everywhere. Do **not** set `TELEMETRY_TEST_MODE`,
`TELEMETRY_SANITIZER_DISABLED`, `CHECKOUT_TEST_FAULT`, `PAYMENT_TEST_FAULT`, `SHIPPING_TEST_FAULT` or
`CHECKOUT_LEASE_MS` in the cluster. `ACCOUNT_BACKEND=local` keeps the previous Redis/Postgres-in-assistant
flow available as a rollback.

## 3. Proposed rollout (needs approval at every step)

Images: build `linux/amd64` with `--provenance=false`, push, and pin each by digest in the manifests. The
base-image digest in `src/accountservice/Dockerfile` is already pinned; the `redis:7.2-alpine` tag in
`order-queue.yaml` must be pinned by digest first.

1. Back up: `pg_dump` of the accounts database (migration 0002+ add columns and constraints; 0005 adds
   `orders`). Take the dump from the live Postgres only after approval.
2. `create-secrets.sh`, then apply `order-queue.yaml`; wait for `order-queue-0` Ready; confirm
   `redis-cli config get appendfsync` = `always` and `maxmemory-policy` = `noeviction`.
3. Apply `accountservice.yaml`. On first start it runs the migrations under an advisory lock; watch the log for
   `accounts.migration.up_to_date` with `app.account.migration.version=0005`. Existing accounts keep working:
   migration 0002 backfills `email_display` and the first/last name split, marks existing rows
   `profile_version = 1` (legacy: never rejected for a missing phone), and legacy addresses that do not satisfy
   the contract stay legacy-only (they surface as the "complete your profile" banner).
4. Roll payment, shipping, checkout (in that order), then frontend and assistant with the env above.
   Checkout without `ORDER_QUEUE_ADDR` logs a warning and runs non-durably; with it set, a Redis outage makes
   `PlaceOrder` fail closed (`order_queue_unavailable`) rather than charge without a record.
5. Run the loadgenerator with `CUSTOMER_USERS_WEIGHT=1` for a few minutes.

Rollback: set `ACCOUNT_BACKEND=local` on frontend and assistant and redeploy them (accounts remain in
Postgres; both backends use the same `auth:{sha256(token)}` session keys, so signed-in users stay signed in).
Checkout/payment/shipping can stay on the new images; with `ORDER_QUEUE_ADDR` unset they behave as before.
Migrations are additive and are not rolled back.

## 4. Checkout failure modes (what the shopper sees, what to look at)

| Situation | Result | Recovery |
|---|---|---|
| Same key submitted twice (double click, reload) | second call returns the first order (`replayed`) | none needed |
| Same key, different cart/address/card | `idempotency_conflict` | shopper restarts checkout |
| Another worker holds the lease | `checkout_in_progress` | retry in a moment |
| Card declined | `payment_declined`; intent row removed | pick another card |
| Payment outcome unknown (timeout, crash) | `payment_uncertain`; the retry resolves it with `GetCharge`, **never charges twice** | retry with the same key |
| Order queue down | `order_queue_unavailable` before any charge; after a charge the order still completes | retry |
| Charged but history publish fails | success with `record_status=history_unrecorded` | republish loop + janitor + reconciler rebuild it from `co:{key}` |
| Postgres down | checkout is unaffected; history is written when it returns | messages stay pending; infrastructure errors are never dead-lettered |
| Poison message | moved to `orders:dead` with a reason code after 5 deliveries | inspect `XRANGE orders:dead - +`, fix, replay manually |

Useful commands (read-only): `redis-cli -h order-queue XLEN orders:v1`, `XPENDING orders:v1 accountservice`,
`XLEN orders:dead`, `HGETALL co:<key>` (contains the plan, not card data or tokens).

## 5. Observe verification after deploy (proposed)

Use the Observe MCP / UI against the **staging** tenant only.

1. Services present with `service.version` = the release id: accountservice, frontend, checkoutservice,
   paymentservice, shippingservice, shoppingassistantservice.
2. One signed-in checkout, then in Trace Explorer find the `hipstershop.CheckoutService/PlaceOrder` span with
   `app.checkout.idempotency_status=new` and `app.customer.authenticated=true`; confirm the single trace spans
   frontend, checkout, payment, shipping, email and the Redis CLIENT spans (APM should now show `order-queue` as a
   dependency and `accounts-db`).
3. Find `orders.consume` (CONSUMER) for that `app.order.id`; confirm it has a **link** to `orders.publish.completed`.
4. A replayed submit shows `idempotency_status=replayed`; `app.orders.recorded` increments once per order.
5. PII check: search spans, logs and metrics for the synthetic customers' emails, `tok_demo_`, and 16-digit
   numbers; expect zero hits. Confirm `app.session.id`, `app.customer.email`, `app.customer.name` and
   `app.customer.email_hash` no longer appear on assistant spans.
6. Re-key anything that joined on the old `app.customer.id` (= session id) values; see
   `docs/account-experience-telemetry.md` section 2.
7. Alerts worth creating: `app.orders.outbox.dead_total > 0`; `record_status=history_unrecorded` rate;
   `app.account.db.outcome != ok`.

## 6. Local verification

`bash src/accountservice/tests/run_all.sh` (several minutes). Needs: `/tmp/obvenv311` (the accountservice
requirements plus `pgserver`, `pyyaml`), `/tmp/lgvenv2` (locust), `redis-server`, Go, and Node (paymentservice
`npm ci --ignore-scripts`). No Docker, cluster or network access. It builds the real frontend, checkout, shipping
and payment binaries and runs them against a private Postgres and AOF Redis with a local OTLP receiver.
