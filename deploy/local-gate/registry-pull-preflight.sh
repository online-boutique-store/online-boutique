#!/usr/bin/env bash
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/guard.sh"
require_tool docker
require_tool kubectl
assert_local
require_local_docker_context
PROBE_TAG="$LOCAL_REGISTRY/ob-gate/pull-probe:$(git -C "$REPO_ROOT" rev-parse --short HEAD)"
TMP_DIR=/tmp/ob-gate/pull-probe
mkdir -p "$TMP_DIR"
cat > "$TMP_DIR/Dockerfile" <<'EOF'
FROM alpine:3.20
CMD ["sh", "-c", "test \"$LOCAL_GATE_PROBE\" = passed"]
EOF
docker build -q -t "$PROBE_TAG" "$TMP_DIR" >/dev/null
docker push "$PROBE_TAG" >/dev/null
k -n "$GATE_NAMESPACE" delete pod local-registry-pull-probe --ignore-not-found >/dev/null 2>&1 || true
k -n "$GATE_NAMESPACE" run local-registry-pull-probe --image="$PROBE_TAG" --image-pull-policy=Always --restart=Never --env=LOCAL_GATE_PROBE=passed --command -- sh -c 'test "$LOCAL_GATE_PROBE" = passed'
k -n "$GATE_NAMESPACE" wait --for=jsonpath='{.status.phase}'=Succeeded pod/local-registry-pull-probe --timeout=180s
phase="$(k -n "$GATE_NAMESPACE" get pod local-registry-pull-probe -o jsonpath='{.status.phase}')"
[[ "$phase" == Succeeded ]] || fail "registry pull probe phase: $phase"
if k -n "$GATE_NAMESPACE" get events --field-selector involvedObject.name=local-registry-pull-probe -o wide | grep -E 'ErrImagePull|ImagePullBackOff' >/dev/null; then
  fail "registry pull probe reported image pull failure"
fi
k -n "$GATE_NAMESPACE" delete pod local-registry-pull-probe --ignore-not-found >/dev/null
printf 'LOCAL_REGISTRY_PULL_PREFLIGHT_PASS image=%s\n' "$PROBE_TAG"
