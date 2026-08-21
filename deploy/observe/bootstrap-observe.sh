#!/usr/bin/env bash
# DEPRECATED for fresh deploys: use `kubectl apply -k deploy/observe` instead
# — it applies the collector AND instruments every service in one declarative
# step, rather than deploying plain manifests and patching them afterward.
# Kept only for clusters already bootstrapped the old way. See
# deploy/observe/README.md.
#
# Wire Observe telemetry on a fresh EKS cluster after deploy/aws/deploy.sh.
#
# Uses the standalone OTEL collector in this repo (same Observe ingest token as before).
# Alternatively, install the Observe Kubernetes agent from the Observe UI and run
# patch-tracing-env.sh without the OTEL_COLLECTOR_* overrides.
#
# Usage:
#   ./deploy/observe/bootstrap-observe.sh
#   ./deploy/observe/bootstrap-observe.sh --with-loadgenerator

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
WITH_LOADGEN=false

for arg in "$@"; do
  case "$arg" in
    --with-loadgenerator) WITH_LOADGEN=true ;;
  esac
done

echo "==> Deploying Observe OTEL collector..."
kubectl apply -f "${ROOT}/deploy/observe/observe-otel-collector.yaml"

echo "==> Waiting for collector DaemonSet..."
kubectl rollout status daemonset/observe-otel-collector --timeout=180s

echo "==> Patching microservices for OTLP export..."
OTEL_COLLECTOR_ADDR=observe-otel-collector:4317 \
OTEL_COLLECTOR_OTLP=http://observe-otel-collector:4317 \
  "${ROOT}/deploy/observe/patch-tracing-env.sh"

echo "==> Waiting for instrumented rollouts..."
for svc in frontend cartservice checkoutservice adservice; do
  kubectl rollout status "deployment/${svc}" --timeout=300s || true
done

if [[ "${WITH_LOADGEN}" == "true" ]]; then
  echo "==> Deploying load generator..."
  "${ROOT}/deploy/observe/deploy-loadgenerator.sh"
fi

echo ""
echo "Observe bootstrap complete."
echo "  Collector: observe-otel-collector:4317"
echo "  Verify:    kubectl logs deployment/frontend --tail=3 | grep trace_id"
echo ""
echo "Configure OpenTelemetry Logs correlation tags in Observe:"
echo "  trace_id, k8s.pod.name (filter log_type=application for request logs)"
