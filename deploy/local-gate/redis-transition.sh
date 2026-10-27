#!/usr/bin/env bash
# Boundary-aware Redis transition helper. Only runs against the dedicated kind context.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/guard.sh"
OLD_POD="${OLD_POD:?set OLD_POD to the old Deployment pod}"
NEW_POD="${NEW_POD:?set NEW_POD to the standby StatefulSet pod}"
SERVICE="${SERVICE:-redis-cart}"

assert_local
selector="$(k -n "$GATE_NAMESPACE" get statefulset redis-cart -o jsonpath='{.spec.selector.matchLabels}')"
[[ "$selector" == '{"app":"redis-cart"}' ]] || fail "intended StatefulSet selector changed: $selector"

writers=(cartservice shoppingassistantservice loadgenerator)
for writer in "${writers[@]}"; do
  k -n "$GATE_NAMESPACE" scale deploy/"$writer" --replicas=0 >/dev/null 2>&1 || true
done
for writer in "${writers[@]}"; do
  k -n "$GATE_NAMESPACE" wait --for=jsonpath='{.status.replicas}'=0 deploy/"$writer" --timeout=120s >/dev/null 2>&1 || true
  replicas="$(k -n "$GATE_NAMESPACE" get deploy "$writer" -o jsonpath='{.status.replicas}' 2>/dev/null || printf 0)"
  [[ -z "$replicas" || "$replicas" == 0 ]] || fail "writer still has replicas: $writer=$replicas"
done

# This marker is written only after the final synchronization has been validated.
for _ in $(seq 1 30); do
  state="$(k -n "$GATE_NAMESPACE" get configmap redis-transition-state -o jsonpath='{.data.state}' 2>/dev/null || true)"
  [[ "$state" == new-active-no-writes ]] && break
  sleep 1
done
state="$(k -n "$GATE_NAMESPACE" get configmap redis-transition-state -o jsonpath='{.data.state}' 2>/dev/null || true)"
[[ "$state" == new-active-no-writes ]] || fail "refusing cutover without validated no-write boundary: $state"

old_selector='{"app":"redis-cart","redis-role":"active"}'
new_selector='{"app":"redis-cart","redis-role":"standby"}'
k -n "$GATE_NAMESPACE" patch service "$SERVICE" --type=merge -p "{\"spec\":{\"selector\":$new_selector}}" >/dev/null
for _ in $(seq 1 60); do
  count="$(k -n "$GATE_NAMESPACE" get endpointslice -l kubernetes.io/service-name="$SERVICE" -o jsonpath='{range .items[*].endpoints[*]}{.conditions.ready}{"\n"}{end}' | grep -c true || true)"
  [[ "$count" == 1 ]] && break
  sleep 1
done
count="$(k -n "$GATE_NAMESPACE" get endpointslice -l kubernetes.io/service-name="$SERVICE" -o jsonpath='{range .items[*].endpoints[*]}{.conditions.ready}{"\n"}{end}' | grep -c true || true)"
[[ "$count" == 1 ]] || fail "cutover did not produce exactly one ready endpoint"
printf 'REDIS_NEW_ACTIVE_NO_WRITES\n'
k create configmap redis-transition-state -n "$GATE_NAMESPACE" --from-literal=state=new_writes_accepted --dry-run=client -o yaml | k apply -f - >/dev/null
for writer in "${writers[@]}"; do
  k -n "$GATE_NAMESPACE" scale deploy/"$writer" --replicas=1 >/dev/null 2>&1 || true
done
printf 'REDIS_CUTOVER_COMPLETE write_boundary=new_writes_accepted\n'
