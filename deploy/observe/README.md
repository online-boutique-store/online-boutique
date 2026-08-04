# Observe APM instrumentation for Online Boutique

Instrument the app with OpenTelemetry and forward traces and metrics to [Observe](https://docs.observeinc.com/docs/apm-instrumentation). Application logs are emitted as JSON on stdout and collected by the Observe Kubernetes agent (not via OTLP logs).

> **Deploying fresh?** Install the **Observe Kubernetes agent** in your
> cluster from the Observe UI (Data & integrations > Add data > Kubernetes),
> then run `deploy/aws/deploy.sh` — it deploys the app, with every service
> already pointed at the agent's forwarder, in one step (see
> [Fresh cluster redeploy](#fresh-cluster-redeploy-new-aws-account--eks)
> below). No Observe credentials live in this repo at all — the agent holds
> its own ingest token from its own install flow. Everything under "Manual /
> legacy instrumentation flow" describes the older imperative flow
> (`patch-tracing-env.sh`, `bootstrap-observe.sh`), kept only for clusters
> already bootstrapped that way.

## Architecture

```
Microservices ──OTLP/gRPC──► observe-agent-forwarder.observe.svc.cluster.local:4317 ──► Observe
Microservices ──stdout JSON──► Observe Kubernetes agent (log collection) ──► Observe
```

`deploy/observe/kustomization.yaml` layers on top of `deploy/aws/kustomization.yaml` (which layers on `kubernetes-manifests/` and rewrites image names to your ECR repo) — it does **not** reference `kubernetes-manifests/` directly, because doing so would apply the OTEL patches over bare `image: frontend`-style names and produce `ErrImagePull` on EKS. `deploy/observe` = `deploy/aws` + OTEL patches, pointed at the Observe agent's in-cluster forwarder Service.

## Fresh cluster redeploy (new AWS account / EKS)

From a machine with `aws`, `eksctl`, `docker`, and `kubectl` configured for the new account:

```bash
# 1. Install the Observe Kubernetes agent (Observe UI: Data & integrations >
#    Add data > Kubernetes) — this creates the `observe` namespace and the
#    observe-agent-forwarder Service that apps send OTLP to. Can be done
#    before or after step 2; no telemetry arrives until it's installed.

# 2. ECR + EKS + build/push images + apply manifests, already instrumented
#    (~20–30 min)
./deploy/aws/deploy.sh

# 3. Optional: load generator for continuous traffic
./deploy/observe/deploy-loadgenerator.sh
```

`deploy/aws/deploy.sh` auto-detects the AWS account ID and creates `online-boutique` ECR + EKS in `us-east-1`. Override with `AWS_REGION`, `EKS_CLUSTER_NAME`, or `ECR_REPO_NAME` if needed. It always applies `deploy/observe` (app + OTEL instrumentation, with images rewritten to ECR); to deploy without any OTEL env vars, apply `deploy/aws` directly instead.

Each service gets one small patch (`patches/*.yaml`) adding `ENABLE_TRACING=1`, the forwarder endpoint, `OTEL_SERVICE_NAME`, `K8S_POD_NAME`/`K8S_NAMESPACE` (log correlation), and `imagePullPolicy: Always`. adservice's patch also activates its baked-in OTel Java agent. Verify with:

```bash
kubectl rollout status deployment/frontend
kubectl logs deployment/frontend --tail=5 | grep trace_id
```

**Updating `service.version` for deployment markers:** it defaults to `1.0.0` in the `OTEL_RESOURCE_ATTRIBUTES` literal of `kustomization.yaml`. To stamp a real version (e.g. from CI), edit that literal — e.g. to `$GIT_SHA` — as part of your release step, then re-apply. This stays a declarative manifest change, not a live `kubectl set env` against a running cluster.

> **Standalone / generic OTel collector:** a self-hosted `otelcol-contrib` DaemonSet with a richer processing pipeline (RED metrics, team-attribution, span filtering) lives on the `feat/generic-otel-collection` branch if you need it.

**Applying `deploy/observe` outside of `deploy.sh`:** it depends on `deploy/aws/kustomization.yaml` having real values in place of `ECR_URI_PLACEHOLDER` (normally substituted transiently by `deploy.sh` via a `sed` pipe, never written to disk). Applying it standalone without that substitution will deploy pods with a literal, non-existent image registry.

## Manual / legacy instrumentation flow

The steps below patch a plain, already-deployed cluster after the fact with `kubectl set env` / `kubectl patch`. Kept for existing clusters bootstrapped this way before the Kustomize setup above existed — prefer `kubectl apply -k deploy/observe` for anything new.

## 1. Prerequisites

- EKS cluster with Online Boutique deployed
- Observe Kubernetes agent installed in the `observe` namespace with an OTLP forwarder at `observe-agent-forwarder.observe.svc.cluster.local:4317`

## 2. Rebuild and push images

All instrumented services must be rebuilt for `linux/amd64` after code changes. Set your ECR URI:

```bash
ECR_URI="YOUR_ACCOUNT.dkr.ecr.us-east-1.amazonaws.com/online-boutique"

for entry in \
  "frontend:src/frontend" \
  "checkoutservice:src/checkoutservice" \
  "productcatalogservice:src/productcatalogservice" \
  "shippingservice:src/shippingservice" \
  "adservice:src/adservice" \
  "cartservice:src/cartservice/src" \
  "paymentservice:src/paymentservice" \
  "currencyservice:src/currencyservice" \
  "emailservice:src/emailservice" \
  "recommendationservice:src/recommendationservice"
do
  IMAGE="${entry%%:*}"
  CONTEXT="${entry#*:}"
  docker buildx build --platform linux/amd64 -t "${ECR_URI}:${IMAGE}" --push "${CONTEXT}"
done
```

## 3. Enable tracing and log correlation env vars

```bash
chmod +x deploy/observe/patch-tracing-env.sh
./deploy/observe/patch-tracing-env.sh
```

This script sets, for every microservice deployment:

- `ENABLE_TRACING=1`, OTLP endpoint, service name, traces + metrics exporters
- `OTEL_LOGS_EXPORTER=none` (logs via stdout → K8s agent, not OTLP)
- `K8S_POD_NAME` / `K8S_NAMESPACE` via downward API (pod-level log correlation)
- `imagePullPolicy: Always` (required when pushing to fixed image tags)

For adservice it also sets the pinned OpenTelemetry Java agent via `JAVA_TOOL_OPTIONS`.

## Instrumentation by language

| Service | Language | Approach |
|---------|----------|----------|
| adservice | Java | Pinned `opentelemetry-javaagent.jar` + Log4j `JsonTemplateLayout` + gRPC log correlation interceptor |
| cartservice | .NET | NuGet OTEL SDK + Redis instrumentation + correlated JSON console logger |
| paymentservice, currencyservice | Node.js | Explicit `otel.js` (`NodeSDK` + `GrpcInstrumentation` before OTLP exporters) + pino correlation mixin |
| emailservice, recommendationservice | Python | `otel_setup.py` + correlated JSON logger |
| frontend, checkoutservice, productcatalogservice, shippingservice | Go | OTEL SDK + logrus `tracehook.go` + `log.WithContext(ctx)` on request paths |

## Log correlation in Observe

Application stdout logs include two correlation dimensions:

| Field | When present | Observe use |
|-------|--------------|-------------|
| `trace_id`, `span_id`, `log_type=application` | Inside an active request span | Pivot trace → related logs |
| `k8s.pod.name`, `k8s.namespace.name` | All application logs | Pivot trace → all pod logs (startup, health, etc.) |

On the **OpenTelemetry Logs** dataset, configure correlation tags:

- `trace_id` → trace/span pivot
- `k8s.pod.name` → pod-level pivot

Filter request-correlated logs with `log_type = application` (or `trace_id is not null`).

## Verify

```bash
kubectl get pods -n observe
kubectl logs deployment/frontend --tail=5 | grep trace_id
kubectl logs deployment/adservice --tail=5 | grep trace_id
```

Generate traffic via the frontend LoadBalancer, then check **Traces**, **Service Explorer**, and **OpenTelemetry Logs** in Observe.

## Continuous synthetic traffic (load generator)

Online Boutique includes a [Locust](https://locust.io/) load generator that simulates shopping flows (browse products, add to cart, checkout, change currency). It calls the **in-cluster** `frontend` service — not the external LoadBalancer.

```bash
chmod +x deploy/observe/deploy-loadgenerator.sh
./deploy/observe/deploy-loadgenerator.sh

# Heavier load (if the cluster has headroom):
USERS=10 ./deploy/observe/deploy-loadgenerator.sh
```

Watch aggregated request stats:

```bash
kubectl logs -f deployment/loadgenerator
```

Stop synthetic traffic:

```bash
kubectl delete deployment loadgenerator
```

Default load is **5 concurrent users** with low CPU requests so it fits a 2-node `t3.medium` cluster.

## Demo: investigate a bad deployment

Step-by-step presenter script for simulating a misconfigured `REDIS_ADDR` on cartservice, then using Observe deployment markers, traces, and correlated logs to find the root cause:

**[deploy/observe/DEMO.md](./DEMO.md)**

Quick commands:

```bash
# Prerequisites: load generator running, Observe instrumentation applied
./deploy/observe/deploy-loadgenerator.sh   # if needed

# Inject incident (bad Redis host)
./deploy/observe/demo-break.sh

# ... investigate in Observe ...

# Restore
./deploy/observe/demo-fix.sh
```

Use `demo-break.sh` / `demo-fix.sh` instead of applying the legacy YAML manifests directly — the YAML files replace the entire Deployment and strip OTEL env vars.

## Notes

- **redis-cart** appears as a dependency edge from cartservice in the service map, not as its own APM service tile.
- CPU-constrained clusters (e.g. 2× t3.medium) may stall rollouts; delete old pending pods if needed.
- Bump `OTEL_JAVAAGENT_VERSION` in `src/adservice/Dockerfile` to upgrade the Java agent.
