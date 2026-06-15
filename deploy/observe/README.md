# Observe APM instrumentation for Online Boutique

Instrument the app with OpenTelemetry and forward traces and metrics to [Observe](https://docs.observeinc.com/docs/apm-instrumentation). Application logs are emitted as JSON on stdout and collected by the Observe Kubernetes agent (not via OTLP logs).

## Architecture

```
Microservices ──OTLP/gRPC──► observe-otel-collector:4317 ──► Observe (traces + metrics)
Microservices ──stdout JSON──► filelog receiver ──► Observe Kubernetes Explorer (logs)
```

The repo includes an optional standalone `observe-otel-collector.yaml` if you are not using the Observe Kubernetes agent.

## Fresh cluster redeploy (new AWS account / EKS)

From a machine with `aws`, `eksctl`, `docker`, and `kubectl` configured for the new account:

```bash
# 1. ECR + EKS + build/push images + apply manifests (~20–30 min)
./deploy/aws/deploy.sh

# 2. Observe collector + OTEL env vars + optional load generator
./deploy/observe/bootstrap-observe.sh --with-loadgenerator
```

`deploy/aws/deploy.sh` auto-detects the AWS account ID and creates `online-boutique` ECR + EKS in `us-east-1`. Override with `AWS_REGION`, `EKS_CLUSTER_NAME`, or `ECR_REPO_NAME` if needed.

If you use the **Observe Kubernetes agent** instead of the standalone collector, install it from the Observe UI first, then run only `patch-tracing-env.sh` (no `OTEL_COLLECTOR_*` overrides).

## 1. Prerequisites

- EKS cluster with Online Boutique deployed
- Observe Kubernetes agent installed in the `observe` namespace with an OTLP forwarder at `observe-agent-forwarder.observe.svc.cluster.local:4317` **or** the standalone collector from `observe-otel-collector.yaml`

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

Optional: deploy the standalone collector instead of using the Observe agent forwarder:

```bash
# Edit observe-otel-collector.yaml with your Observe ingest token first
kubectl apply -f deploy/observe/observe-otel-collector.yaml
OTEL_COLLECTOR_ADDR=observe-otel-collector:4317 \
OTEL_COLLECTOR_OTLP=http://observe-otel-collector:4317 \
  ./deploy/observe/patch-tracing-env.sh
```

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
