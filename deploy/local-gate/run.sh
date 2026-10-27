#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/guard.sh"
require_tool skaffold
require_tool docker
require_local_docker_context
PLAN_ONLY=0
for arg in "$@"; do [[ "$arg" == --plan ]] && PLAN_ONLY=1; done
run() { if (( PLAN_ONLY )); then printf '+ '; printf '%q ' "$@"; printf '\n'; else "$@"; fi; }

if (( PLAN_ONLY )); then
  printf '%s\n' "KUBECONFIG=$KUBECONFIG" "CONTEXT=kind-$CLUSTER_NAME" "REGISTRY=$LOCAL_REGISTRY" "AWS credentials cleared" \
    "registry-pull-preflight" "skaffold build --default-repo=$LOCAL_REGISTRY --push --file-output=/tmp/ob-gate/build.json" \
    "scenario-a" "scenario-b" "amd64-smoke" "report" "cluster-down"
  exit 0
fi

case "${1:---plan}" in
  --plan) "$0" --plan ;;
  registry-preflight) "$SCRIPT_DIR/cluster-up.sh"; "$SCRIPT_DIR/registry-pull-preflight.sh" ;;
  build)
    assert_local
    "$SCRIPT_DIR/registry-pull-preflight.sh"
    skaffold build --default-repo="$LOCAL_REGISTRY" --push --file-output=/tmp/ob-gate/build.json
    assert_no_remote_images /tmp/ob-gate/build.json
    ;;
  all)
    "$SCRIPT_DIR/cluster-up.sh"
    "$SCRIPT_DIR/registry-pull-preflight.sh"
    "$0" build
    printf 'LOCAL_GATE_IMPLEMENTATION_PENDING: scenario scripts have not yet been wired\n' >&2
    exit 2
    ;;
  *) printf 'usage: run.sh [--plan|registry-preflight|build|all]\n' >&2; exit 2 ;;
esac
