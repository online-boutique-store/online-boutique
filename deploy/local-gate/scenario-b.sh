#!/usr/bin/env bash
# Scenario B (reduced scope, see report): old Redis Deployment -> new StatefulSet in namespace ob-upgrade.
set -uo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$SCRIPT_DIR/lib/guard.sh"
NS=ob-upgrade
LOG=/tmp/ob-gate/scenario-b.log
: > "$LOG"
pass=0; failn=0
ok()  { echo "PASS $*" | tee -a "$LOG"; pass=$((pass+1)); }
bad() { echo "FAIL $*" | tee -a "$LOG"; failn=$((failn+1)); }
chk() { local label="$1"; shift; if "$@" >/dev/null 2>&1; then ok "$label"; else bad "$label"; fi; }
rc()  { k -n $NS exec "$1" -- redis-cli --raw "${@:2}"; }
endpoints() { k -n $NS get endpointslice -l kubernetes.io/service-name=redis-cart -o json | jq -r '[.items[].endpoints[]?|select(.conditions.ready==true)|.targetRef.name]|sort|join(",")'; }
fp() { # type-aware, TTL-bucketed fingerprint of every key (values never printed)
  local pod="$1"
  for key in $(rc "$pod" --scan | sort); do
    t=$(rc "$pod" type "$key"); ttl=$(rc "$pod" ttl "$key"); b=persist; [ "$ttl" -gt 0 ] && b=ttl
    case "$t" in string) v=$(rc "$pod" get "$key");; hash) v=$(rc "$pod" hgetall "$key");; *) v=$(rc "$pod" dump "$key");; esac
    printf '%s %s %s %s\n' "$key" "$t" "$b" "$(printf '%s' "$v" | shasum -a 256 | cut -c1-16)"
  done
}
assert_local

k -n $NS delete deploy,sts,svc,pvc,cm --all --ignore-not-found >/dev/null 2>&1
# ---- baseline: HEAD's Deployment (emptyDir) + Service
k -n $NS apply -f /tmp/ob-gate/old-redis.yaml >/dev/null
k -n $NS rollout status deploy/redis-cart --timeout=180s >/dev/null
OLD=$(k -n $NS get pod -l app=redis-cart -o jsonpath='{.items[0].metadata.name}')
for i in 1 2 3 4 5; do rc "$OLD" set "cart:sess$i" "{\"items\":[{\"p\":\"6E92ZMYYFZ\",\"q\":$i}]}" >/dev/null; done
for i in 1 2; do rc "$OLD" setex "auth:synthetic$i" 1800 "{\"email\":\"seed$i@example.com\"}" >/dev/null; done
rc "$OLD" setex activation_handoff:synthetic 900 '{"email":"seed@example.com"}' >/dev/null
for i in 1 2 3; do rc "$OLD" set "account:legacy$i" '{"legacy":true}' >/dev/null; done
rc "$OLD" hset cartservice:meta k v >/dev/null
OLD_FP=$(fp "$OLD"); OLD_N=$(rc "$OLD" dbsize)
echo "$OLD_FP" > /tmp/ob-gate/redis-old.fp
[ "$OLD_N" = 12 ] && ok "baseline seeded: $OLD_N keys in old Deployment pod" || bad "baseline key count $OLD_N"
LEG=$(rc "$OLD" --scan --pattern 'account:*' | wc -l | tr -d ' ')
echo "REDIS_ACCOUNT_MIGRATION_DEFERRED legacy_account_keys=$LEG" | tee -a "$LOG"

# ---- naive apply: reproduce the conflict (expected finding, NOT a pass)
k -n $NS apply -f /tmp/ob-gate/new-redis-sts.yaml >/dev/null
k -n $NS wait --for=condition=Ready pod/redis-cart-0 --timeout=240s >/dev/null 2>&1
EPS=$(endpoints); N=$(echo "$EPS" | tr ',' '\n' | grep -c .)
[ "$N" -ge 2 ] && echo "CONFLICT_REPRODUCED (expected finding): Service redis-cart has $N endpoints: $EPS" | tee -a "$LOG" || bad "naive apply did not reproduce conflict (endpoints=$EPS)"
k -n $NS delete sts redis-cart --wait=true >/dev/null; k -n $NS delete pvc redis-data-redis-cart-0 --ignore-not-found >/dev/null
[ "$(rc "$OLD" dbsize)" = 12 ] && ok "old Deployment data intact after conflict cleanup" || bad "old data changed"

# ---- isolation: label the EXISTING pod (not the template: that would restart it and wipe emptyDir)
k -n $NS label pod "$OLD" redis-role=active --overwrite >/dev/null
k -n $NS patch svc redis-cart --type=merge -p '{"spec":{"selector":{"app":"redis-cart","redis-role":"active"}}}' >/dev/null
sleep 3
[ "$(endpoints)" = "$OLD" ] && ok "Service isolated to old pod (selector app+redis-role=active)" || bad "service endpoints: $(endpoints)"
# standby StatefulSet: selector untouched, only the POD TEMPLATE gets redis-role=standby
jq '.spec.template.metadata.labels["redis-role"]="standby"' <(ruby -ryaml -rjson -e 'puts YAML.load_file("/tmp/ob-gate/new-redis-sts.yaml").to_json') > /tmp/ob-gate/new-redis-sts-standby.json
k -n $NS apply -f /tmp/ob-gate/new-redis-sts-standby.json >/dev/null
k -n $NS wait --for=condition=Ready pod/redis-cart-0 --timeout=240s >/dev/null
SEL=$(k -n $NS get sts redis-cart -o jsonpath='{.spec.selector.matchLabels}')
[ "$SEL" = '{"app":"redis-cart"}' ] && ok "StatefulSet spec.selector unchanged: $SEL" || bad "selector changed: $SEL"
[ "$(endpoints)" = "$OLD" ] && ok "standby pod is NOT behind the Service" || bad "standby leaked into Service: $(endpoints)"

# ---- writer inventory + freeze
WR=$(k -n $NS get deploy,sts -o json | jq -r '[.items[]|select(.metadata.name!="redis-cart")|.metadata.name]|join(",")')
[ -z "$WR" ] && ok "writer inventory: no cartservice/assistant/loadgenerator workloads exist in ob-upgrade (carts/sessions are seeded synthetic keys)" || bad "unexpected workloads: $WR"
CL=$(rc "$OLD" client list | grep -v 'cmd=client' | grep -vc 'flags=S' || true)
echo "old-instance application clients before sync: $CL (redis-cli exec only)" | tee -a "$LOG"

# ---- replicate, validate
OLD_IP=$(k -n $NS get pod "$OLD" -o jsonpath='{.status.podIP}')
rc redis-cart-0 replicaof "$OLD_IP" 6379 >/dev/null
for _ in $(seq 1 60); do rc redis-cart-0 info replication | grep -q 'master_link_status:up' && break; sleep 1; done
rc redis-cart-0 info replication | grep -q 'master_link_status:up' && ok "replication link up" || bad "replication link not up"
rc redis-cart-0 info replication | grep -q 'master_sync_in_progress:0' && ok "initial sync complete" || bad "sync in progress"
NEW_FP=$(fp redis-cart-0)
[ "$NEW_FP" = "$OLD_FP" ] && ok "key/type/ttl-bucket/value fingerprints identical (12 keys)" || { bad "fingerprint mismatch"; diff <(echo "$OLD_FP") <(echo "$NEW_FP") | head -20 | tee -a "$LOG"; }
rc redis-cart-0 replicaof no one >/dev/null
rc redis-cart-0 bgrewriteaof >/dev/null
for _ in $(seq 1 60); do rc redis-cart-0 info persistence | grep -q 'aof_rewrite_in_progress:0' && break; sleep 1; done
[ "$(rc redis-cart-0 info replication | grep -c role:master)" = 1 ] && ok "standby promoted (role:master), still Service-isolated" || bad "promotion"

# ---- cutover with no writes; lossless rollback test BEFORE any write
k -n $NS create cm redis-transition-state --from-literal=state=new-active-no-writes >/dev/null
k -n $NS patch svc redis-cart --type=merge -p '{"spec":{"selector":{"app":"redis-cart","redis-role":"standby"}}}' >/dev/null; sleep 3
[ "$(endpoints)" = "redis-cart-0" ] && ok "cutover: exactly one endpoint = redis-cart-0" || bad "cutover endpoints: $(endpoints)"
# pre-write rollback (lossless)
k -n $NS patch svc redis-cart --type=merge -p '{"spec":{"selector":{"app":"redis-cart","redis-role":"active"}}}' >/dev/null; sleep 3
[ "$(endpoints)" = "$OLD" ] && [ "$(fp "$OLD")" = "$OLD_FP" ] && [ "$(fp redis-cart-0)" = "$OLD_FP" ] && ok "PRE-WRITE ROLLBACK is lossless (old authoritative, both fingerprints equal)" || bad "pre-write rollback not lossless"
k -n $NS patch svc redis-cart --type=merge -p '{"spec":{"selector":{"app":"redis-cart","redis-role":"standby"}}}' >/dev/null; sleep 3

# ---- first write accepted by new instance = the boundary
k -n $NS patch cm redis-transition-state --type=merge -p '{"data":{"state":"new_writes_accepted"}}' >/dev/null
rc redis-cart-0 set cart:post-cutover-sentinel '{"items":[{"p":"OLJCESPC7Z","q":1}]}' >/dev/null
[ "$(rc "$OLD" exists cart:post-cutover-sentinel)" = 0 ] && ok "boundary crossed: sentinel exists only on new instance (old lacks it)" || bad "sentinel on old"
rollback_allowed() { [ "$(k -n $NS get cm redis-transition-state -o jsonpath='{.data.state}')" != new_writes_accepted ]; }
if rollback_allowed; then bad "rollback to old Redis still allowed after writes accepted"; else ok "POST-WRITE ROLLBACK REFUSED (ROLLBACK_NOT_LOSSLESS_AFTER_WRITE_BOUNDARY); forward recovery only"; fi

# ---- finish: delete old, restore intended selector, persistence
k -n $NS delete deploy redis-cart >/dev/null
k -n $NS patch svc redis-cart --type=merge -p '{"spec":{"selector":{"app":"redis-cart"}}}' >/dev/null
k -n $NS patch svc redis-cart --type=json -p '[{"op":"remove","path":"/spec/selector/redis-role"}]' >/dev/null 2>&1 || true
sleep 4
SELV=$(k -n $NS get svc redis-cart -o jsonpath='{.spec.selector}')
[ "$SELV" = '{"app":"redis-cart"}' ] && ok "Service selector restored to intended $SELV" || bad "service selector $SELV"
[ "$(endpoints)" = "redis-cart-0" ] && ok "final: one Redis workload, one endpoint (redis-cart-0)" || bad "final endpoints $(endpoints)"
[ "$(k -n $NS get deploy,sts -l app=redis-cart -o name | wc -l | tr -d ' ')" = 1 ] && ok "exactly one Redis workload object" || bad "workload count"
k -n $NS delete pod redis-cart-0 >/dev/null; k -n $NS wait --for=condition=Ready pod/redis-cart-0 --timeout=240s >/dev/null
[ "$(rc redis-cart-0 dbsize)" = 13 ] && [ "$(rc redis-cart-0 exists cart:post-cutover-sentinel)" = 1 ] && ok "all 12 seeded keys + post-cutover sentinel survive pod restart (AOF on PVC)" || bad "data after restart: $(rc redis-cart-0 dbsize)"
echo "SCENARIO_B pass=$pass fail=$failn" | tee -a "$LOG"
[ "$failn" = 0 ]
