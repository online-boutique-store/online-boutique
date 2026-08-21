#!/usr/bin/env bash
# Clean reinstall of Observe K8s agent with APM RED metrics enabled.
#
# Usage:
#   OBSERVE_TOKEN='your:token' ./deploy/observe/reinstall-observe.sh
#   ./deploy/observe/reinstall-observe.sh   # reads token from observe-otel-credentials

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"

OBSERVE_TOKEN="${OBSERVE_TOKEN:-ds1xwLGe048d8jQkD2dz:a94WbtG91SrLlknE1E5m5NLtKEuWP69q}"
OBSERVE_COLLECTION_ENDPOINT="${OBSERVE_COLLECTION_ENDPOINT:-https://145056927297.collect.observeinc.com}"
CLUSTER_NAME="${CLUSTER_NAME:-online-boutique}"
DEPLOYMENT_ENV="${DEPLOYMENT_ENV:-production}"

echo "==> Observe endpoint: ${OBSERVE_COLLECTION_ENDPOINT}"
echo "==> Cluster:           ${CLUSTER_NAME}"
echo "==> Environment:       ${DEPLOYMENT_ENV}"

echo "==> Removing standalone collector (if any)..."
kubectl delete daemonset observe-otel-collector svc observe-otel-collector \
  secret observe-otel-credentials configmap observe-otel-collector-config \
  -n default --ignore-not-found 2>/dev/null || true

echo "==> Uninstalling existing Observe agent..."
helm uninstall observe-agent -n observe 2>/dev/null || true
sleep 5

echo "==> Updating default-namespace credentials (for scripts)..."
kubectl create namespace default --dry-run=client -o yaml | kubectl apply -f -
kubectl create secret generic observe-otel-credentials -n default \
  --from-literal=OBSERVE_TOKEN="${OBSERVE_TOKEN}" \
  --from-literal=OBSERVE_OTEL_ENDPOINT="${OBSERVE_COLLECTION_ENDPOINT}/v2/otel" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "==> Helm repo..."
helm repo add observe https://observeinc.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update observe

echo "==> observe namespace + agent secret..."
kubectl create namespace observe --dry-run=client -o yaml | kubectl apply -f -
kubectl -n observe create secret generic agent-credentials \
  --from-literal=OBSERVE_TOKEN="${OBSERVE_TOKEN}" \
  --dry-run=client -o yaml | kubectl apply -f -
kubectl annotate secret agent-credentials -n observe \
  meta.helm.sh/release-name=observe-agent \
  meta.helm.sh/release-namespace=observe \
  --overwrite
kubectl label secret agent-credentials -n observe \
  app.kubernetes.io/managed-by=Helm \
  --overwrite

echo "==> Installing Observe agent (RED metrics ON for APM)..."
helm upgrade --install observe-agent observe/agent -n observe \
  --set observe.collectionEndpoint.value="${OBSERVE_COLLECTION_ENDPOINT}" \
  --set observe.token.create=false \
  --set cluster.name="${CLUSTER_NAME}" \
  --set cluster.deploymentEnvironment.name="${DEPLOYMENT_ENV}" \
  --set application.REDMetrics.enabled=true \
  --set application.REDMetrics.onlyGenerateForAPMSpans=true \
  --set node.forwarder.metrics.outputFormat=otel \
  --wait --timeout 15m

echo "==> Waiting for forwarder..."
kubectl rollout status daemonset/observe-agent-forwarder-agent -n observe --timeout=300s

echo "==> Patching app OTLP → observe-agent-forwarder..."
"${ROOT}/deploy/observe/patch-tracing-env.sh"

echo "==> Rolling all instrumented services..."
for svc in adservice cartservice checkoutservice currencyservice emailservice frontend \
  paymentservice productcatalogservice recommendationservice shippingservice; do
  kubectl rollout status "deployment/${svc}" --timeout=300s || true
done

echo ""
echo "Reinstall complete."
echo "  Token:     (new token applied)"
echo "  Forwarder: observe-agent-forwarder.observe.svc.cluster.local:4317"
echo "  APM:       REDMetrics.enabled=true"
echo ""
echo "Wait 2-5 min for load, then refresh APM Services (environment=production)."
