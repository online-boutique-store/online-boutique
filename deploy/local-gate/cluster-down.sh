#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/guard.sh"
require_tool kind
assert_local
k -n "$GATE_NAMESPACE" scale deploy/loadgenerator --replicas=0 >/dev/null 2>&1 || true
kind delete cluster --name "$CLUSTER_NAME" --kubeconfig "$KUBECONFIG"
docker rm -f "$REGISTRY_CONTAINER" ob-amd64-pg ob-amd64-redis ob-amd64-asst ob-amd64-fe >/dev/null 2>&1 || true
docker network rm ob-amd64 >/dev/null 2>&1 || true
rm -rf /tmp/ob-gate
printf 'LOCAL_GATE_CLEANUP_COMPLETE\n'
