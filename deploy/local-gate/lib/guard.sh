#!/usr/bin/env bash
# Shared safety boundary for the local kind gate. Source this file; never use bare kubectl.
set -euo pipefail

if [[ -n "${BASH_VERSION:-}" ]]; then
  _GUARD_SOURCE="${BASH_SOURCE[0]}"
else
  _GUARD_SOURCE="${(%):-%N}"
fi
GATE_ROOT="$(cd "$(dirname "$_GUARD_SOURCE")/.." && pwd)"
REPO_ROOT="$(cd "$GATE_ROOT/../.." && pwd)"
source "$GATE_ROOT/versions.env"
export KUBECONFIG="${KUBECONFIG:-$KUBECONFIG_PATH}"
export AWS_CONFIG_FILE=/dev/null
export AWS_SHARED_CREDENTIALS_FILE=/dev/null
unset AWS_PROFILE AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_SESSION_TOKEN AWS_SECURITY_TOKEN || true

fail() { printf 'LOCAL_GATE_ERROR: %s\n' "$*" >&2; exit 1; }

assert_local() {
  [[ -n "${KUBECONFIG:-}" && "$KUBECONFIG" == "$KUBECONFIG_PATH" ]] || fail "unexpected kubeconfig: ${KUBECONFIG:-unset}"
  local current server
  current="$(kubectl --kubeconfig "$KUBECONFIG" config current-context 2>/dev/null || true)"
  [[ "$current" == "kind-$CLUSTER_NAME" ]] || fail "unexpected Kubernetes context: $current"
  server="$(kubectl --kubeconfig "$KUBECONFIG" config view --raw --minify -o jsonpath='{.clusters[0].cluster.server}' 2>/dev/null || true)"
  [[ "$server" =~ ^https://127\.0\.0\.1:[0-9]+$ ]] || fail "Kubernetes API is not loopback: $server"
  [[ "$server" != *eks.amazonaws.com* && "$server" != *amazonaws.com* ]] || fail "refusing AWS Kubernetes endpoint: $server"
}

k() {
  assert_local
  kubectl --kubeconfig "$KUBECONFIG" --context "kind-$CLUSTER_NAME" "$@"
}

assert_local_registry() {
  local value="$1"
  [[ "$value" != *dkr.ecr.* && "$value" != *amazonaws.com* ]] || fail "remote registry reference: $value"
  [[ "$value" == "$LOCAL_REGISTRY"/* || "$value" == kind-registry:* || "$value" != */*.*/* ]] || fail "non-local registry reference: $value"
}

assert_no_remote_images() {
  local file="$1"
  [[ -f "$file" ]] || fail "manifest file missing: $file"
  if grep -E 'dkr\.ecr\.|amazonaws\.com|gcr\.io/|ghcr\.io/' "$file" >/dev/null; then
    fail "remote registry reference found in $file"
  fi
}

require_tool() { command -v "$1" >/dev/null 2>&1 || fail "required tool not found: $1"; }

require_local_docker_context() {
  local endpoint
  endpoint="$(docker context inspect --format '{{.Endpoints.docker.Host}}' 2>/dev/null || true)"
  [[ "$endpoint" == unix:///Users/*/.docker/run/docker.sock || "$endpoint" == unix:///var/run/docker.sock ]] || fail "unexpected Docker context endpoint: $endpoint"
}
