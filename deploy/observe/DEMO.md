# Observe APM demo: bad deployment → trace → logs → root cause

**Scenario:** A cartservice rollout accidentally points `REDIS_ADDR` at a hostname that does not exist. Cart operations fail; checkout error rate climbs. You use Observe APM deployment markers, traces, and correlated logs to find and fix the misconfiguration.

**Duration:** ~10–15 minutes  
**Prerequisites:** Cluster instrumented per [README.md](./README.md), load generator running, Observe log correlation tags configured on the OpenTelemetry Logs dataset.

---

## Cast of characters

| Service | Role in the incident |
|---------|----------------------|
| **loadgenerator** | Steady shopping traffic (browse, add to cart, checkout) |
| **frontend** | User-facing HTTP; shows elevated errors on `/cart`, `/cart/checkout` |
| **cartservice** | **Root cause** — cannot reach Redis at `redis-cart:9999` (wrong port) |
| **checkoutservice** | Cascading failures when PlaceOrder calls cartservice |
| **redis-cart** | Healthy Redis (unchanged); cartservice is simply pointed at the wrong host |

---

## Before the demo (5 min)

1. Confirm baseline is healthy:

```bash
kubectl get deployment cartservice loadgenerator
kubectl logs deployment/loadgenerator --tail=5   # ~0% failures
```

2. Start load generator if not running:

```bash
./deploy/observe/deploy-loadgenerator.sh
```

3. In Observe, open **Service Explorer** and note:
   - Error rate ~0% on `cartservice`, `frontend`, `checkoutservice`
   - `cartservice` → `redis-cart` dependency edge on the service map

4. Confirm log correlation tags on **OpenTelemetry Logs**: `trace_id`, `k8s.pod.name`, filter `log_type = application`.

---

## Deployment markers in Observe APM

Observe creates **deployment markers** when it sees a **new `service.version`** value on incoming trace spans (OpenTelemetry resource attribute). Kubernetes annotations alone do not trigger markers.

| Phase | `service.version` | Set by |
|-------|-------------------|--------|
| Baseline | `1.0.0` | `patch-tracing-env.sh` |
| Broken deploy | `1.0.1-broken-redis` | `demo-break.sh` |
| Fix / rollback | `1.0.2-fixed-redis` | `demo-fix.sh` |

In **Act 2**, point at the marker on the cartservice chart and note the version change in the deployments table.

Override versions if re-running the demo:

```bash
DEMO_BROKEN_VERSION=1.0.3-broken-redis ./deploy/observe/demo-break.sh
DEMO_FIXED_VERSION=1.0.4-fixed-redis ./deploy/observe/demo-fix.sh
```

---

## Act 1 — Inject the incident (1 min)

```bash
chmod +x deploy/observe/demo-break.sh deploy/observe/demo-fix.sh
./deploy/observe/demo-break.sh
```

**What happens technically:**
- Only `REDIS_ADDR` changes to `redis-cart:9999` (wrong port on the real host); OTEL and log-correlation env vars stay intact
- Pods roll; `ConnectionMultiplexer` uses `AbortOnConnectFail=false` (set in code), so startup succeeds
- gRPC health checks still pass (`Ping()` does not hit Redis), so pods stay Ready and the service map topology is preserved
- Cart RPCs fail at runtime with Redis connection/timeout errors; frontend and checkoutservice show cascading errors

Wait ~2 minutes for the load generator to hit failing cart/checkout paths.

---

## Act 2 — Detect in Observe APM (3 min)

**Talking points:**

1. **Service Explorer → cartservice**
   - Error rate climbs on the APM chart
   - **Deployment marker** appears when `service.version` changes to `1.0.1-broken-redis` (check deployments table)

2. **Broaden the view**
   - `frontend` and `checkoutservice` error rates rise (cascading failure)
   - Service map still shows cartservice; Redis dependency may show errors on the cartservice → redis edge

3. **Filter time range** to the deployment marker window

---

## Act 3 — Drill into a trace (3 min)

1. Open **Traces** (or trace list from cartservice / frontend service page)
2. Filter: `status = error` (or error rate panel → view traces)
3. Pick a failed **checkout** or **add to cart** trace

**Expected trace shape:**

```
frontend  →  checkoutservice.PlaceOrder  →  cartservice.GetCart / AddItem  ✗
```

4. Click the **failed cartservice span**
   - Status: error / FailedPrecondition
   - Attributes: gRPC status FailedPrecondition, Redis client span showing connection failure to `redis-cart:9999`

**Talking point:** Traces show *where* it broke and *which dependency*, not always *why* the config is wrong.

---

## Act 4 — Correlate logs to root cause (3 min)

From the failed span detail page:

1. Open **Logs related to trace/span** (trace correlation via `trace_id`)
2. Filter `log_type = application` if needed

**Look for cartservice log lines like:**

```json
{
  "severity": "Error",
  "message": "Redis cart storage failed ...",
  "trace_id": "...",
  "log_type": "application",
  "k8s.pod.name": "cartservice-..."
}
```

3. Message includes Redis connection failure details (`Can't access cart storage`, socket/timeout errors)

**Optional — pod-level logs:** Pivot on `k8s.pod.name` to see all logs from the new cartservice pod (startup, env context).

4. **Root cause narrative:**
   > "The deployment rolled at [marker time]. cartservice is trying to reach Redis on port 9999 instead of 6379. Someone misconfigured `REDIS_ADDR` in the deployment."

5. Cross-check deployment annotation (kubectl or K8s metadata in Observe):

```bash
kubectl get deployment cartservice -o jsonpath='{.metadata.annotations.deployment\.change}{"\n"}'
```

---

## Act 5 — Fix and verify recovery (2 min)

```bash
./deploy/observe/demo-fix.sh
```

**In Observe:**
- Second **deployment marker** when `service.version` changes to `1.0.2-fixed-redis`
- Error rate returns to baseline
- Failed trace rate drops; load generator stats show checkout succeeding again

```bash
kubectl logs deployment/loadgenerator --tail=10   # failures/s back to 0
```

---

## Demo script (one-liner cues)

| Step | Say | Do |
|------|-----|-----|
| 1 | "We have steady synthetic shoppers adding items and checking out." | Show loadgenerator logs, healthy APM |
| 2 | "A new cartservice version just rolled out." | Run `demo-break.sh` |
| 3 | "Error rate jumped — notice the deployment marker." | Service Explorer, cartservice chart |
| 4 | "Checkout is failing downstream. Let's open a trace." | Error trace frontend → checkoutservice → cartservice |
| 5 | "The cartservice span failed talking to Redis. Logs tied to this trace_id show the connection error." | Trace → correlated logs |
| 6 | "Root cause: bad REDIS_ADDR in the deployment. Roll back." | Run `demo-fix.sh`, show recovery |

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| No errors after break | Ensure loadgenerator is running; wait 2–3 min; check `kubectl logs deployment/cartservice` |
| No deployment marker | Ensure `service.version` changed on cartservice spans; re-run `demo-break.sh` with a new `DEMO_BROKEN_VERSION` |
| Logs not on trace tab | Configure `trace_id` correlation tag on OpenTelemetry Logs dataset |
| Rollout stuck Pending | CPU pressure on small cluster — delete old pending pod: `kubectl delete pod -l app=cartservice --field-selector=status.phase=Pending` |
| OTEL stopped after break | You applied the old full-replacement YAML — re-run `./patch-tracing-env.sh` and use `demo-break.sh` instead |

---

## Files

| File | Purpose |
|------|---------|
| `demo-break.sh` | Set bad `REDIS_ADDR`, annotate deployment, rollout |
| `demo-fix.sh` | Restore `redis-cart:6379`, annotate deployment, rollout |
| `demo-bad-cartservice.yaml` | **Deprecated** — full Deployment replace strips OTEL env; use `demo-break.sh` |
| `demo-rollback-cartservice.yaml` | **Deprecated** — use `demo-fix.sh` |

---

## Custom Span Attributes

Every microservice stamps a set of `app.*` span attributes onto its spans so traces can be sliced by business dimensions in Observe. The attributes are propagated across services via W3C Baggage, so each span carries every attribute that exists at that point in the request flow.

| Attribute | Meaning | Origin |
|-----------|---------|--------|
| `app.session.id` | Browser session (the user identity — no login in this app) | frontend (cookie UUID) |
| `app.request.id` | Per-HTTP-request UUID | frontend |
| `app.currency` | User's selected currency code | frontend (cookie) |
| `app.product.id` | Product being viewed / added | frontend route |
| `app.product.quantity` | Quantity added to cart | frontend add-to-cart |
| `app.cart.action` | Cart operation: `add` / `view` / `empty` / `checkout` | frontend route |
| `app.order.id` | Order UUID | checkout (generated in PlaceOrder) |
| `app.order.items` | Comma-joined product IDs in the order | checkout |
| `app.payment.transaction_id` | Payment transaction UUID | paymentservice |
| `app.payment.card_type` | `visa` / `mastercard` | paymentservice (stays on payment span only) |
| `app.shipping.tracking_id` | Shipment tracking ID | shippingservice |

**Observe queries (OPAL examples):**
- Group spans by `app.session.id` to see all activity for one user across every service.
- `app.cart.action = "checkout"` with `app.order.id` set = purchased; `app.cart.action = "add"` with no subsequent checkout = abandoned in cart.
- Filter `app.product.id` to follow a product through the view → cart → checkout funnel.

**Propagation note:** `OTEL_PROPAGATORS=tracecontext,baggage` is set by `patch-tracing-env.sh` so the .NET and Java runtimes (which rely on the env var) extract baggage. Go, Python, and Node services also register the Baggage propagator in code. Attributes created mid-flow (`app.order.id`, `app.payment.transaction_id`, `app.shipping.tracking_id`) only appear on spans that execute at or after they are created; `app.payment.card_type` is not returned by the payment service and so only appears on the payment span.
