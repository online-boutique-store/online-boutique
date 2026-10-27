# Release record: account experience (uncommitted WIP on c267cbc9, branch `account-experience`)

**Status: NOT DEPLOYED. NOT PUSHED.** Local implementation and tests only. No images built, no cluster,
Observe, AWS or live-database changes. Namespace for any future rollout: `online-boutique` only.

## Scope (plan v7, milestones M0-M7)

- M0 checkpoint commit c267cbc9; branch `account-experience`; secrets audit. M1 read-only aggregate counts.
- M2-M4 accountservice (Python gRPC, Postgres migrations 0001-0005), shared validation contract, dashboard,
  profile, addresses, signup validation across web/assistant/loadgen (password rules unchanged).
- M5 durable idempotent checkout (order-queue Redis state machine, idempotent payment/shipping, two-phase
  `orders:v1` stream, consumer, reconciler, janitor), order history, guest-order claim on sign-in.
- M6 catalog-only demo-card tokenization, saved payment methods, saved-card checkout.
- M7 assistant `list_orders`/`get_order` + saved cards + PII-free telemetry, loadgen `CustomerUser`, docs.

## New deploy artifacts (written, not applied)

- `deploy/accountservice/accountservice.yaml` - accountservice Deployment/Service.
- `deploy/accountservice/order-queue.yaml` - AOF `appendfsync always`, `noeviction` Redis StatefulSet.
  Pin `redis:7.2-alpine` by digest before apply.
- `deploy/accountservice/create-secrets.sh` - `accountservice-credentials` (never prints values).

## Behaviour changes that affect Observe

Assistant telemetry no longer exports `app.session.id`, `app.customer.email`, `app.customer.name`,
`app.customer.email_hash`, or `app.customer.id` = session id. `app.customer.id` is now the Postgres UUID
(signed-in only) and `app.session.ref` an HMAC reference. Log `UserId`/`session` hold the reference. Queries
and reference-table joins on the old values need re-keying (see docs/account-experience-telemetry.md).

## Local test results at release time

`bash src/accountservice/tests/run_all.sh`: validation 122, migrations 56, accountservice 94, sanitizer
layers 19, frontend e2e 89, loadgen identities 3, loadgen CustomerUser 15, payment methods 70, checkout
durability 102 (5 consecutive green runs), plus 7 assistant suites. Known intermittent failure:
`shoppingassistantservice/tests/test_accounts_pg.py::test_duplicates_and_concurrency` (psycopg pool timeout
under 20-way concurrency); reproduced on the unmodified checkpoint c267cbc9 (2/8 runs), so pre-existing.

## Images

None built. When approved: `linux/amd64`, `--provenance=false`, push, pin by digest, set `service.version`
consistently across services.
