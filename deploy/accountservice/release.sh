#!/usr/bin/env bash
# Account-experience release, run BY A HUMAN one stage at a time (the coding agent cannot run docker,
# aws or kubectl on this machine). Namespace online-boutique only; images linux/amd64, pushed to ECR
# with --provenance=false and deployed BY DIGEST. Every stage is idempotent and prints what it changes.
#
#   ./deploy/accountservice/release.sh preflight     read-only checks
#   ./deploy/accountservice/release.sh build         build + push 8 images, write digests to $OUT/digests.env
#   ./deploy/accountservice/release.sh backup        pg_dump of the accounts DB to $OUT/accounts-<ts>.dump
#   ./deploy/accountservice/release.sh infra         secrets + order-queue Redis (pinned by digest)
#   CONFIRM_MIGRATIONS=yes ./deploy/accountservice/release.sh accountservice
#                                                    deploys accountservice: RUNS MIGRATIONS 0002-0005 ON LIVE POSTGRES
#   ./deploy/accountservice/release.sh backends      payment -> shipping -> checkout -> email (durable checkout on)
#   ./deploy/accountservice/release.sh frontends     frontend + assistant cut over to ACCOUNT_BACKEND=grpc
#   ./deploy/accountservice/release.sh loadgen       loadgenerator image with CustomerUser at weight 1
#   ./deploy/accountservice/release.sh status        read-only rollout/queue/migration status
#   ./deploy/accountservice/release.sh rollback      frontend + assistant back to ACCOUNT_BACKEND=local
set -euo pipefail
NS=online-boutique
CTX="arn:aws:eks:us-west-2:739672403694:cluster/online-boutique"
REG="739672403694.dkr.ecr.us-west-2.amazonaws.com"
REPO="$REG/online-boutique"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="${OUT:-$ROOT/deploy/releases/out}"
TAG="$(git -C "$ROOT" rev-parse --short HEAD)"
SERVICES=(accountservice frontend checkoutservice paymentservice shippingservice emailservice shoppingassistantservice loadgenerator)
K() { kubectl --context "$CTX" -n "$NS" "$@"; }
mkdir -p "$OUT"

need_digests() {
  [ -f "$OUT/digests.env" ] || { echo "run the build stage first ($OUT/digests.env missing)"; exit 1; }
  # shellcheck disable=SC1091
  . "$OUT/digests.env"
}
img() { local v="DIGEST_${1//-/_}"; echo "$REPO/$1@${!v}"; }

# Strategic-merge env into the `server` container (merges by env name; other env untouched).
env_plain() {  # deploy NAME=value ...
  local d=$1; shift; local items=""
  for kv in "$@"; do items+="{\"name\":\"${kv%%=*}\",\"value\":\"${kv#*=}\"},"; done
  K patch deploy "$d" --type=strategic -p "{\"spec\":{\"template\":{\"spec\":{\"containers\":[{\"name\":\"server\",\"env\":[${items%,}]}]}}}}"
}
env_secret() {  # deploy ENVNAME KEY
  K patch deploy "$1" --type=strategic -p "{\"spec\":{\"template\":{\"spec\":{\"containers\":[{\"name\":\"server\",\"env\":[{\"name\":\"$2\",\"valueFrom\":{\"secretKeyRef\":{\"name\":\"accountservice-credentials\",\"key\":\"$3\"}}}]}]}}}}"
}
roll() {  # deploy image
  K set image "deploy/$1" "server=$2"
  K rollout status "deploy/$1" --timeout=300s
}

case "${1:-}" in
preflight)
  echo "context: $(kubectl config current-context)  (expected $CTX)"
  aws sts get-caller-identity --query Account --output text
  docker buildx version
  K get deploy,sts -o wide
  K get secret accounts-db-credentials accountservice-credentials 2>&1 | sed 's/^/  /'
  git -C "$ROOT" status --short | head
  echo "HEAD=$TAG";;

build)
  aws ecr get-login-password --region us-west-2 | docker login --username AWS --password-stdin "$REG"
  : > "$OUT/digests.env"
  for s in "${SERVICES[@]}"; do
    aws ecr describe-repositories --region us-west-2 --repository-names "online-boutique/$s" >/dev/null 2>&1 \
      || aws ecr create-repository --region us-west-2 --repository-name "online-boutique/$s" >/dev/null
    ctx="$ROOT/src/$s"
    docker buildx build --platform linux/amd64 --provenance=false --push \
      -t "$REPO/$s:$TAG" --metadata-file "$OUT/$s.meta.json" "$ctx"
    d=$(python3 -c "import json,sys;print(json.load(open(sys.argv[1]))['containerimage.digest'])" "$OUT/$s.meta.json")
    echo "DIGEST_${s//-/_}=$d" >> "$OUT/digests.env"
    echo "$s -> $REPO/$s@$d"
  done
  rd=$(docker buildx imagetools inspect redis:7.2-alpine --format '{{json .Manifest}}' \
        | python3 -c "import json,sys;m=json.load(sys.stdin);print([x['digest'] for x in m['manifests'] if x['platform']['architecture']=='amd64' and x['platform']['os']=='linux'][0])")
  echo "REDIS_DIGEST=$rd" >> "$OUT/digests.env"
  echo "redis:7.2-alpine linux/amd64 -> $rd"; echo "wrote $OUT/digests.env";;

backup)
  f="$OUT/accounts-$(date +%Y%m%d%H%M%S).dump"
  K exec sts/accounts-db -c postgres -- sh -c 'pg_dump -U "$POSTGRES_USER" -Fc accounts' > "$f"
  ls -l "$f"; echo "verify: pg_restore --list $f | head";;

infra)
  need_digests
  NAMESPACE=$NS bash "$ROOT/deploy/accountservice/create-secrets.sh"
  sed "s#image: redis:7.2-alpine.*#image: redis@${REDIS_DIGEST}#" "$ROOT/deploy/accountservice/order-queue.yaml" | K apply -f -
  K rollout status sts/order-queue --timeout=300s
  K exec order-queue-0 -- redis-cli config get appendfsync
  K exec order-queue-0 -- redis-cli config get maxmemory-policy;;

accountservice)
  need_digests
  [ "${CONFIRM_MIGRATIONS:-}" = yes ] || { echo "This applies migrations 0002-0005 to the LIVE accounts DB. Run 'backup' first, then re-run with CONFIRM_MIGRATIONS=yes."; exit 1; }
  sed "s#image: accountservice.*#image: $(img accountservice)#" "$ROOT/deploy/accountservice/accountservice.yaml" | K apply -f -
  K rollout status deploy/accountservice --timeout=300s
  K logs deploy/accountservice --tail=200 | grep -E 'accounts.migration' | tail -5;;

backends)
  need_digests
  for d in paymentservice shippingservice; do
    env_plain "$d" "ORDER_QUEUE_ADDR=order-queue:6379"; env_secret "$d" TELEMETRY_REF_KEY TELEMETRY_REF_KEY
    roll "$d" "$(img "$d")"
  done
  env_plain checkoutservice "ORDER_QUEUE_ADDR=order-queue:6379" "ACCOUNT_SERVICE_ADDR=accountservice:8081"
  env_secret checkoutservice ACCOUNT_SERVICE_SECRET CHECKOUT_SECRET
  env_secret checkoutservice CHECKOUT_FP_KEY CHECKOUT_FP_KEY
  env_secret checkoutservice TELEMETRY_REF_KEY TELEMETRY_REF_KEY
  roll checkoutservice "$(img checkoutservice)"
  roll emailservice "$(img emailservice)";;

frontends)
  need_digests
  env_plain frontend "ACCOUNT_BACKEND=grpc" "ACCOUNT_SERVICE_ADDR=accountservice:8081"
  env_secret frontend ACCOUNT_SERVICE_SECRET FRONTEND_SECRET
  env_secret frontend TELEMETRY_REF_KEY TELEMETRY_REF_KEY
  roll frontend "$(img frontend)"
  env_plain shoppingassistantservice "ACCOUNT_BACKEND=grpc" "ACCOUNT_SERVICE_ADDR=accountservice:8081"
  env_secret shoppingassistantservice ACCOUNT_SERVICE_SECRET ASSISTANT_SECRET
  env_secret shoppingassistantservice TELEMETRY_REF_KEY TELEMETRY_REF_KEY
  roll shoppingassistantservice "$(img shoppingassistantservice)";;

loadgen)
  need_digests
  K set env deploy/loadgenerator -c main CUSTOMER_USERS_WEIGHT=1      # its container is `main`, not `server`
  K set image deploy/loadgenerator "main=$(img loadgenerator)"
  K rollout status deploy/loadgenerator --timeout=300s;;

status)
  K get deploy accountservice frontend checkoutservice paymentservice shippingservice emailservice shoppingassistantservice loadgenerator \
    -o custom-columns=NAME:.metadata.name,READY:.status.readyReplicas,IMAGE:.spec.template.spec.containers[0].image
  K exec order-queue-0 -- redis-cli XLEN orders:v1 || true
  K exec order-queue-0 -- redis-cli XLEN orders:dead || true
  K exec order-queue-0 -- redis-cli XINFO GROUPS orders:v1 || true;;

rollback)
  env_plain frontend "ACCOUNT_BACKEND=local"; K rollout status deploy/frontend --timeout=300s
  env_plain shoppingassistantservice "ACCOUNT_BACKEND=local"; K rollout status deploy/shoppingassistantservice --timeout=300s
  echo "frontend + assistant back on the local account backend (sessions are compatible).";;

*) sed -n '2,20p' "$0"; exit 1;;
esac
