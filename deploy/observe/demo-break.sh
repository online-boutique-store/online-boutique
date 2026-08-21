#!/usr/bin/env bash
# Simulate a bad release: cartservice gets a misconfigured Redis endpoint.
# Bumps service.version on cartservice, checkoutservice, and frontend so
# Observe APM shows deployment markers on all three — errors appear on
# checkoutservice (cascading from cartservice), and the trace leads back
# to cartservice's Redis misconfiguration as the root cause.
#
# Uses wrong PORT (redis-cart:9999), not a wrong hostname, so:
#   - cartservice pods stay Ready (gRPC health does not touch Redis)
#   - the service map topology stays intact (cartservice + redis-cart remain)
#   - cart/checkout RPCs fail at runtime with visible error spans + logs
#
# Usage: ./deploy/observe/demo-break.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=patch-tracing-env.sh
source "${SCRIPT_DIR}/patch-tracing-env.sh"

BROKEN_VERSION="${DEMO_BROKEN_VERSION:-1.0.3-broken-redis}"
# Wrong port on the real redis-cart host — keeps DNS + dependency edges valid.
BROKEN_REDIS_ADDR="${DEMO_BROKEN_REDIS_ADDR:-redis-cart:9999}"

preflight() {
  if ! kubectl get deployment cartservice >/dev/null 2>&1; then
    echo "ERROR: cartservice deployment not found."
    exit 1
  fi
  local tracing
  tracing="$(kubectl get deployment cartservice -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="ENABLE_TRACING")].value}' 2>/dev/null || true)"
  if [[ "${tracing}" != "1" ]]; then
    echo "ERROR: cartservice does not have ENABLE_TRACING=1."
    echo "Run ./deploy/observe/patch-tracing-env.sh first."
    exit 1
  fi
}

wait_ready() {
  local svc="$1"
  kubectl rollout status "deployment/${svc}" --timeout=120s || {
    echo "ERROR: ${svc} rollout did not complete."
    kubectl get pods -l "app=${svc}" -o wide || true
    exit 1
  }
  local ready
  ready="$(kubectl get deployment "${svc}" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)"
  if [[ "${ready}" -lt 1 ]]; then
    echo "ERROR: ${svc} has no ready replicas."
    exit 1
  fi
}

preflight

echo "==> Deploying broken release (version=${BROKEN_VERSION})"
echo "    cartservice : REDIS_ADDR -> ${BROKEN_REDIS_ADDR} (wrong port, pods stay Ready)"
echo "    checkoutservice + frontend: version bump only"

# --- cartservice: bad Redis + version bump ---
kubectl annotate deployment/cartservice \
  deployment.version="${BROKEN_VERSION}" \
  deployment.change="misconfigured REDIS_ADDR — wrong port 9999 instead of 6379" \
  --overwrite

kubectl set env deployment/cartservice \
  "REDIS_ADDR=${BROKEN_REDIS_ADDR}" \
  "OTEL_RESOURCE_ATTRIBUTES=$(otel_resource_attributes "${BROKEN_VERSION}")"

# --- checkoutservice: version bump only (no functional change) ---
kubectl annotate deployment/checkoutservice \
  deployment.version="${BROKEN_VERSION}" \
  deployment.change="release ${BROKEN_VERSION}" \
  --overwrite

kubectl set env deployment/checkoutservice \
  "OTEL_RESOURCE_ATTRIBUTES=$(otel_resource_attributes "${BROKEN_VERSION}")"

# --- frontend: version bump only ---
kubectl annotate deployment/frontend \
  deployment.version="${BROKEN_VERSION}" \
  deployment.change="release ${BROKEN_VERSION}" \
  --overwrite

kubectl set env deployment/frontend \
  "OTEL_RESOURCE_ATTRIBUTES=$(otel_resource_attributes "${BROKEN_VERSION}")"

echo "==> Waiting for rollouts..."
for svc in cartservice checkoutservice frontend; do
  wait_ready "${svc}"
done

applied_redis="$(kubectl get deployment cartservice -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="REDIS_ADDR")].value}')"
if [[ "${applied_redis}" != "${BROKEN_REDIS_ADDR}" ]]; then
  echo "ERROR: REDIS_ADDR is '${applied_redis}', expected '${BROKEN_REDIS_ADDR}'."
  exit 1
fi

echo ""
echo "Incident injected."
echo "  service.version=${BROKEN_VERSION} on cartservice, checkoutservice, frontend"
echo "  cartservice REDIS_ADDR=${BROKEN_REDIS_ADDR}"
echo ""
echo "Demo flow:"
echo "  1. Service map — topology unchanged; frontend + checkoutservice error rate rises"
echo "  2. Drill into checkoutservice Errors — see Redis cart errors"
echo "  3. Drill into checkoutservice Deployments — see marker at ${BROKEN_VERSION}"
echo "  4. Click a failing trace — follow it to cartservice Redis span"
echo "  5. Correlate logs — see Redis connection error as root cause"
echo ""
echo "Wait ~2 min with loadgenerator running, then refresh Observe APM."
echo "Restore with: ./deploy/observe/demo-fix.sh"
