#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/guard.sh"
require_tool kind
require_tool docker
require_tool kubectl
require_local_docker_context
mkdir -p /tmp/ob-gate/telemetry

if docker ps --format '{{.Names}}' | grep -qx "$REGISTRY_CONTAINER"; then :
elif docker ps -a --format '{{.Names}}' | grep -qx "$REGISTRY_CONTAINER"; then docker start "$REGISTRY_CONTAINER" >/dev/null
else docker run -d --restart=no --name "$REGISTRY_CONTAINER" -p 127.0.0.1:5001:5000 registry:2 >/dev/null
fi

if kind get clusters 2>/dev/null | grep -qx "$CLUSTER_NAME"; then
  assert_local
else
  kind create cluster --name "$CLUSTER_NAME" --kubeconfig "$KUBECONFIG" --config "$SCRIPT_DIR/kind-config.yaml" --image "$KIND_NODE_IMAGE"
  docker network connect kind "$REGISTRY_CONTAINER" 2>/dev/null || true
fi
assert_local
k create namespace "$GATE_NAMESPACE" --dry-run=client -o yaml | k apply -f - >/dev/null
k create namespace "$OBSERVE_NAMESPACE" --dry-run=client -o yaml | k apply -f - >/dev/null
k apply -f "$SCRIPT_DIR/vendor/calico-v3.28.2.yaml" >/dev/null
k -n kube-system rollout status daemonset/calico-node --timeout=300s
k apply -f "$SCRIPT_DIR/vendor/local-path-provisioner.yaml" >/dev/null
k apply -f "$SCRIPT_DIR/infra/base.yaml" >/dev/null
k -n "$GATE_NAMESPACE" wait --for=jsonpath='{.status.phase}'=Active namespace/"$GATE_NAMESPACE" --timeout=30s
printf 'LOCAL_GATE_CLUSTER_READY context=%s kubeconfig=%s registry=%s\n' "kind-$CLUSTER_NAME" "$KUBECONFIG" "$LOCAL_REGISTRY"
