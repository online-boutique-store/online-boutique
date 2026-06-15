#!/usr/bin/env bash
# Restore the broken release: fix cartservice Redis and bump service.version
# on all three services so Observe APM shows recovery markers.
#
# Usage: ./deploy/observe/demo-fix.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=patch-tracing-env.sh
source "${SCRIPT_DIR}/patch-tracing-env.sh"

FIXED_VERSION="${DEMO_FIXED_VERSION:-1.0.4-fixed-redis}"
GOOD_REDIS_ADDR="${DEMO_GOOD_REDIS_ADDR:-redis-cart:6379}"

preflight() {
  if ! kubectl get deployment cartservice >/dev/null 2>&1; then
    echo "ERROR: cartservice deployment not found."
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
}

preflight

echo "==> Deploying fix (version=${FIXED_VERSION})"
echo "    cartservice : REDIS_ADDR -> ${GOOD_REDIS_ADDR} (restored)"
echo "    checkoutservice + frontend: version bump only"

# --- cartservice: fix Redis + version bump ---
kubectl annotate deployment/cartservice \
  deployment.version="${FIXED_VERSION}" \
  deployment.change="restored correct REDIS_ADDR" \
  --overwrite

kubectl set env deployment/cartservice \
  "REDIS_ADDR=${GOOD_REDIS_ADDR}" \
  "OTEL_RESOURCE_ATTRIBUTES=$(otel_resource_attributes "${FIXED_VERSION}")"

# --- checkoutservice: version bump only ---
kubectl annotate deployment/checkoutservice \
  deployment.version="${FIXED_VERSION}" \
  deployment.change="release ${FIXED_VERSION}" \
  --overwrite

kubectl set env deployment/checkoutservice \
  "OTEL_RESOURCE_ATTRIBUTES=$(otel_resource_attributes "${FIXED_VERSION}")"

# --- frontend: version bump only ---
kubectl annotate deployment/frontend \
  deployment.version="${FIXED_VERSION}" \
  deployment.change="release ${FIXED_VERSION}" \
  --overwrite

kubectl set env deployment/frontend \
  "OTEL_RESOURCE_ATTRIBUTES=$(otel_resource_attributes "${FIXED_VERSION}")"

echo "==> Waiting for rollouts..."
for svc in cartservice checkoutservice frontend; do
  wait_ready "${svc}"
done

applied_redis="$(kubectl get deployment cartservice -o jsonpath='{.spec.template.spec.containers[0].env[?(@.name=="REDIS_ADDR")].value}')"
if [[ "${applied_redis}" != "${GOOD_REDIS_ADDR}" ]]; then
  echo "ERROR: REDIS_ADDR is '${applied_redis}', expected '${GOOD_REDIS_ADDR}'."
  exit 1
fi

echo ""
echo "Release restored."
echo "  service.version=${FIXED_VERSION} on cartservice, checkoutservice, frontend"
echo "  cartservice REDIS_ADDR=${GOOD_REDIS_ADDR}"
echo ""
echo "Observe should show recovery markers and error rate returning to zero."
echo "Verify: kubectl logs deployment/loadgenerator --tail=10"
