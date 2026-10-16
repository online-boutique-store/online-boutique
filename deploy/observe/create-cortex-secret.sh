#!/usr/bin/env bash
# Create/update the `cortex-credentials` Secret the shopping assistant reads.
# The PAT never lands in git, shell history, or a process argument list.
#
#   CORTEX_ACCOUNT_URL=https://<account>.snowflakecomputing.com \
#   ./deploy/observe/create-cortex-secret.sh            # prompts for the PAT (hidden)
#
# or pipe/export it:  CORTEX_PAT=... ./deploy/observe/create-cortex-secret.sh
#
# Use a dedicated demo account/role (SNOWFLAKE.CORTEX_USER or CORTEX_REST_API_USER),
# not a corporate account. Not exercised by CI; review before first use.
set -euo pipefail

NAMESPACE="${NAMESPACE:-online-boutique}"
: "${CORTEX_ACCOUNT_URL:?set CORTEX_ACCOUNT_URL, e.g. https://myorg-myacct.snowflakecomputing.com}"

if [[ -z "${CORTEX_PAT:-}" ]]; then
  read -r -s -p "Cortex PAT (input hidden): " CORTEX_PAT
  echo
fi
[[ -n "$CORTEX_PAT" ]] || { echo "empty PAT" >&2; exit 1; }

BASE_URL="${CORTEX_ACCOUNT_URL%/}/api/v2/cortex/v1"

# --from-file with process substitution keeps the PAT off the command line.
kubectl create secret generic cortex-credentials \
  --namespace "$NAMESPACE" \
  --from-literal=base-url="$BASE_URL" \
  --from-file=pat=<(printf '%s' "$CORTEX_PAT") \
  --dry-run=client -o yaml | kubectl apply -f -

unset CORTEX_PAT
echo "cortex-credentials applied in namespace $NAMESPACE (base-url: $BASE_URL)"
echo "Restart to pick it up:  kubectl -n $NAMESPACE rollout restart deploy/shoppingassistantservice"
