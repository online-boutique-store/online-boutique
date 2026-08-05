#!/usr/bin/env bash
# DEPRECATED for fresh deploys: use `kubectl apply -k deploy/observe` instead,
# which bakes this same env wiring into the manifests so it can't be lost on
# the next plain `kubectl apply`. This script mutates a *running* cluster
# imperatively and is kept only for clusters already bootstrapped the old way.
# See deploy/observe/README.md.
#
# Patch Online Boutique deployments with Observe/OpenTelemetry env vars.
# Enables trace + pod log correlation fields and k8s identity on trace resources.
#
# Usage:
#   ./deploy/observe/patch-tracing-env.sh
#   DEPLOYMENT_ENV=staging ./deploy/observe/patch-tracing-env.sh

set -euo pipefail

COLLECTOR="${OTEL_COLLECTOR_ADDR:-observe-agent-forwarder.observe.svc.cluster.local:4317}"
COLLECTOR_OTLP="${OTEL_COLLECTOR_OTLP:-http://observe-agent-forwarder.observe.svc.cluster.local:4317}"
DEPLOYMENT_ENV="${DEPLOYMENT_ENV:-production}"
SERVICE_VERSION="${SERVICE_VERSION:-1.0.0}"

# OTEL resource attrs shared by patch-tracing-env.sh and demo scripts.
# service.version drives Observe APM deployment markers (must change on each deploy).
otel_resource_attributes() {
  local version="${1:-${SERVICE_VERSION}}"
  # Observe RED metrics group on deployment.environment.name; include both keys.
  echo "deployment.environment=${DEPLOYMENT_ENV},deployment.environment.name=${DEPLOYMENT_ENV},service.version=${version}"
}

SERVICES=(
  adservice
  cartservice
  checkoutservice
  currencyservice
  emailservice
  frontend
  paymentservice
  productcatalogservice
  recommendationservice
  shippingservice
)

patch_k8s_identity() {
  local svc="$1"
  local env_names
  env_names="$(kubectl get deployment "${svc}" -o jsonpath='{.spec.template.spec.containers[0].env[*].name}')"

  if [[ "${env_names}" == *"K8S_POD_NAME"* ]]; then
    return
  fi

  echo "  -> k8s pod identity for ${svc}"
  kubectl patch deployment "${svc}" --type=json -p='[
    {"op":"add","path":"/spec/template/spec/containers/0/env/-","value":{"name":"K8S_POD_NAME","valueFrom":{"fieldRef":{"fieldPath":"metadata.name"}}}},
    {"op":"add","path":"/spec/template/spec/containers/0/env/-","value":{"name":"K8S_NAMESPACE","valueFrom":{"fieldRef":{"fieldPath":"metadata.namespace"}}}}
  ]'
}

patch_service() {
  local svc="$1"
  echo "Patching ${svc}..."

  patch_k8s_identity "${svc}"

  kubectl set env deployment/"${svc}" \
    ENABLE_TRACING=1 \
    COLLECTOR_SERVICE_ADDR="${COLLECTOR}" \
    OTEL_SERVICE_NAME="${svc}" \
    OTEL_EXPORTER_OTLP_ENDPOINT="${COLLECTOR_OTLP}" \
    OTEL_EXPORTER_OTLP_PROTOCOL=grpc \
    OTEL_PROPAGATORS=tracecontext,baggage \
    OTEL_TRACES_EXPORTER=otlp \
    OTEL_METRICS_EXPORTER=otlp \
    OTEL_LOGS_EXPORTER=otlp \
    OTEL_RESOURCE_ATTRIBUTES="$(otel_resource_attributes "${SERVICE_VERSION}")"

  kubectl patch deployment "${svc}" --type=strategic -p='{"spec":{"template":{"spec":{"containers":[{"name":"server","imagePullPolicy":"Always"}]}}}}' 2>/dev/null \
    || kubectl patch deployment "${svc}" --type=strategic -p='{"spec":{"template":{"spec":{"containers":[{"name":"'${svc}'","imagePullPolicy":"Always"}]}}}}' 2>/dev/null \
    || true
}

main() {
  for svc in "${SERVICES[@]}"; do
    patch_service "${svc}"
  done

  kubectl set env deployment/adservice \
    JAVA_TOOL_OPTIONS="-javaagent:/otel/opentelemetry-javaagent.jar" \
    OTEL_INSTRUMENTATION_GRPC_ENABLED=true \
    OTEL_INSTRUMENTATION_LOG4J2_APPENDER_EXPERIMENTAL_LOG_ATTRIBUTES=true

  echo "==> Waiting for rollouts..."
  for svc in "${SERVICES[@]}"; do
    kubectl rollout status "deployment/${svc}" --timeout=300s || true
  done

  echo "Done. All services patched and rolled out."
  echo ""
  echo "Observe APM deployment markers use the OTEL service.version resource attribute."
  echo "Default service.version=${SERVICE_VERSION} — change it on each deploy to trigger a marker."
  echo ""
  echo "Application logs export via OTLP with trace_id/span_id on each log record."
  echo "Configure log-trace correlation in Observe on the OpenTelemetry Logs dataset."
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
  main "$@"
fi
