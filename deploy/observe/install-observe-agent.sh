#!/usr/bin/env bash
# Install the Observe Kubernetes agent and point app OTLP at the forwarder.
#
# Prerequisites: kubectl + helm, cluster admin, ingest token in observe-otel-credentials
# or OBSERVE_TOKEN / OBSERVE_COLLECTION_ENDPOINT env vars.
#
# Usage:
#   ./deploy/observe/install-observe-agent.sh
#   ./deploy/observe/install-observe-agent.sh --patch-apps
#   ./deploy/observe/install-observe-agent.sh --remove-standalone-collector

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CLUSTER_NAME="${CLUSTER_NAME:-online-boutique}"
DEPLOYMENT_ENV="${DEPLOYMENT_ENV:-production}"
PATCH_APPS=false
REMOVE_STANDALONE=false

for arg in "$@"; do
  case "$arg" in
    --patch-apps) PATCH_APPS=true ;;
    --remove-standalone-collector) REMOVE_STANDALONE=true ;;
  esac
done

if [[ -z "${OBSERVE_TOKEN:-}" ]]; then
  OBSERVE_TOKEN="$(kubectl get secret observe-otel-credentials -n default -o jsonpath='{.data.OBSERVE_TOKEN}' 2>/dev/null | base64 -d || true)"
fi
if [[ -z "${OBSERVE_COLLECTION_ENDPOINT:-}" ]]; then
  OBSERVE_COLLECTION_ENDPOINT="$(kubectl get secret observe-otel-credentials -n default -o jsonpath='{.data.OBSERVE_OTEL_ENDPOINT}' 2>/dev/null | base64 -d | sed 's|/v2/otel$||' || true)"
fi

if [[ -z "${OBSERVE_TOKEN}" || -z "${OBSERVE_COLLECTION_ENDPOINT}" ]]; then
  echo "Set OBSERVE_TOKEN and OBSERVE_COLLECTION_ENDPOINT, or apply observe-otel-collector.yaml first."
  exit 1
fi

echo "==> Observe endpoint: ${OBSERVE_COLLECTION_ENDPOINT}"
echo "==> Cluster name:      ${CLUSTER_NAME}"
echo "==> Environment:       ${DEPLOYMENT_ENV}"

echo "==> Adding Observe Helm repo..."
helm repo add observe https://observeinc.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update observe

echo "==> Creating observe namespace and credentials secret..."
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

echo "==> Installing Observe agent (this may take a few minutes)..."
helm upgrade --install observe-agent observe/agent -n observe \
  --set observe.collectionEndpoint.value="${OBSERVE_COLLECTION_ENDPOINT}" \
  --set observe.token.create=false \
  --set cluster.name="${CLUSTER_NAME}" \
  --set cluster.deploymentEnvironment.name="${DEPLOYMENT_ENV}" \
  --set application.REDMetrics.enabled=true \
  --set application.REDMetrics.onlyGenerateForAPMSpans=true \
  --set node.forwarder.metrics.outputFormat=otel \
  --wait --timeout 10m

echo "==> Waiting for OTLP forwarder..."
kubectl rollout status daemonset/observe-agent-forwarder-agent -n observe --timeout=300s 2>/dev/null \
  || kubectl rollout status daemonset -n observe -l app.kubernetes.io/name=forwarder --timeout=300s

FORWARDER="$(kubectl get svc -n observe -o name | grep forwarder | head -1 || true)"
echo "==> Forwarder service: ${FORWARDER:-observe-agent-forwarder}"

if [[ "${REMOVE_STANDALONE}" == "true" ]]; then
  echo "==> Removing standalone observe-otel-collector..."
  kubectl delete daemonset observe-otel-collector --ignore-not-found
  kubectl delete svc observe-otel-collector --ignore-not-found
fi

if [[ "${PATCH_APPS}" == "true" ]]; then
  echo "==> Patching microservices to use observe-agent-forwarder..."
  "${ROOT}/deploy/observe/patch-tracing-env.sh"
fi

echo ""
echo "Observe agent installed."
echo "  OTLP gRPC: observe-agent-forwarder.observe.svc.cluster.local:4317"
echo "  OTLP HTTP: observe-agent-forwarder.observe.svc.cluster.local:4318"
echo ""
echo "Next: ./deploy/observe/patch-tracing-env.sh   # if not already patched"
echo "      kubectl get pods -n observe"
