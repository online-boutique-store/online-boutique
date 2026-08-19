#!/usr/bin/env bash
# Deploys productcatalogservice from the CURRENT tip of origin/feat/observe-otlp-apm.
#
# This is the "fix" invocation: run it after you've landed the fix on
# feat/observe-otlp-apm (a revert of the bug commit, or a follow-up fix PR,
# merged via the GitHub UI same as the break). The script doesn't know or
# care what code is actually at the tip — it just builds and deploys
# whatever is there. It's on you (the operator) to have merged the fix
# first; the script prints the commit it's about to deploy so you can
# eyeball it before it rolls out.
#
# This mirrors demo-break-productcatalog.sh exactly — same branch, same
# build/push/rollout mechanics. The only differences are bookkeeping (which
# "role" gets recorded for the digest-distinctness check below) and the
# words in the two scripts' names, not which git ref gets built.
#
# This script deliberately does NOT write any GitHub-specific data (SHAs,
# commit URLs, repo names) into application telemetry, Kubernetes
# annotations, or image tags. The `deployment.change` annotation is set to
# the commit subject of whatever is actually being deployed — that's plain,
# naturally-occurring commit metadata (not a cross-system identifier), the
# same way any real deploy tool would label a release.
#
# Reliability notes (read this if you're debugging the script, not just
# running it) — this mirrors demo-break-productcatalog.sh:
#
#   1. Source of truth: this script builds from the current tip of
#      origin/feat/observe-otlp-apm in a DEDICATED, isolated git worktree —
#      never from whatever branch happens to be checked out in this working
#      copy. Your current checkout (and any uncommitted work in it) is never
#      touched, even while you're actively switching branches elsewhere to
#      manage PRs.
#
#   2. Cache safety: builds run on a dedicated buildx builder
#      (docker-container driver, isolated BuildKit cache — not your local
#      `docker build` cache) with --no-cache --pull, so every run compiles
#      from source. No possibility of a stale cached layer serving an old
#      binary.
#
#   3. Distinctness guarantee: after pushing, the script compares the new
#      image digest against the last known BROKEN digest (recorded by
#      demo-break-productcatalog.sh in a small state file). If they match,
#      the fix did NOT actually land on the branch — do not trust this
#      rollout. This uses the raw pushed digest only — the image tag stays a
#      plain, non-identifying `:productcatalogservice`.
#
#   4. Runtime verification: after rollout, the script reads back the
#      running pod's actual `imageID` from Kubernetes and confirms it matches
#      the digest that was just pushed — not just that `kubectl rollout
#      status` returned success. This is what catches the "looks fixed but
#      checkout is still broken" scenario for good.
#
# Usage:
#   Merge or revert the fix into feat/observe-otlp-apm via the GitHub UI,
#   THEN:
#   ./deploy/observe/demo-fix-productcatalog.sh
#
# Override version:
#   DEMO_FIXED_PC_VERSION=1.1.1-fixed ./deploy/observe/demo-fix-productcatalog.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${SCRIPT_DIR}/../.." && pwd)"
# shellcheck source=patch-tracing-env.sh
source "${SCRIPT_DIR}/patch-tracing-env.sh"

ROLE="fixed"
BRANCH="feat/observe-otlp-apm"
REGION="${AWS_REGION:-us-west-2}"
REPO_NAME="${ECR_REPO_NAME:-online-boutique}"
BUILDER_NAME="observe-demo-builder"

CACHE_ROOT="${HOME}/.cache/observe-demo"
WORKTREE_DIR="${CACHE_ROOT}/worktrees/fix-productcatalog"
STATE_DIR="${CACHE_ROOT}/state"
STATE_FILE="${STATE_DIR}/productcatalog-digests.env"

# ---------------------------------------------------------------------------

require_cmd() {
  command -v "$1" >/dev/null 2>&1 || { echo "ERROR: '$1' is required but not found in PATH." >&2; exit 1; }
}

for c in git docker aws kubectl; do require_cmd "$c"; done
docker buildx version >/dev/null 2>&1 || { echo "ERROR: docker buildx is not available." >&2; exit 1; }

JSON_TOOL=""
if command -v jq >/dev/null 2>&1; then
  JSON_TOOL=jq
elif command -v python3 >/dev/null 2>&1; then
  JSON_TOOL=python3
else
  echo "ERROR: need 'jq' or 'python3' to parse the buildx metadata file (brew install jq)." >&2
  exit 1
fi

digest_from_metadata() {
  if [[ "${JSON_TOOL}" == jq ]]; then
    jq -r '."containerimage.digest" // empty' "$1"
  else
    python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("containerimage.digest",""))' "$1"
  fi
}

preflight() {
  if ! kubectl get deployment productcatalogservice >/dev/null 2>&1; then
    echo "ERROR: productcatalogservice deployment not found (wrong kube-context?)." >&2
    exit 1
  fi
  if ! git -C "${ROOT}" ls-remote --exit-code --heads origin "${BRANCH}" >/dev/null 2>&1; then
    echo "ERROR: branch '${BRANCH}' not found on origin." >&2
    exit 1
  fi
}

wait_ready() {
  local svc="$1"
  kubectl rollout status "deployment/${svc}" --timeout=180s || {
    echo "ERROR: ${svc} rollout did not complete." >&2
    kubectl get pods -l "app=${svc}" -o wide || true
    exit 1
  }
  local ready
  ready="$(kubectl get deployment "${svc}" -o jsonpath='{.status.readyReplicas}' 2>/dev/null || echo 0)"
  if [[ "${ready}" -lt 1 ]]; then
    echo "ERROR: ${svc} has no ready replicas." >&2
    exit 1
  fi
}

ensure_builder() {
  if ! docker buildx inspect "${BUILDER_NAME}" >/dev/null 2>&1; then
    echo "==> Creating dedicated buildx builder '${BUILDER_NAME}' (isolated cache)..."
    docker buildx create --name "${BUILDER_NAME}" --driver docker-container --bootstrap >/dev/null
  fi
}

# Checks out the CURRENT tip of ${BRANCH}@origin into an isolated, disposable
# worktree. Never touches the branch/state of the working copy this script is
# run from. COMMIT_SHA/COMMIT_SUBJECT are kept only for this script's own
# console output (so you can see, and confirm, what you're about to build) —
# neither is written to Kubernetes or to telemetry.
checkout_worktree() {
  echo "==> Fetching ${BRANCH} from origin..."
  git -C "${ROOT}" fetch origin "${BRANCH}:refs/remotes/origin/${BRANCH}" --quiet

  mkdir -p "$(dirname "${WORKTREE_DIR}")"
  if [[ -d "${WORKTREE_DIR}" ]]; then
    git -C "${ROOT}" worktree remove --force "${WORKTREE_DIR}" 2>/dev/null || rm -rf "${WORKTREE_DIR}"
  fi
  git -C "${ROOT}" worktree prune
  git -C "${ROOT}" worktree add --detach --force "${WORKTREE_DIR}" "origin/${BRANCH}" >/dev/null
  git -C "${WORKTREE_DIR}" clean -fdx >/dev/null

  COMMIT_SHA="$(git -C "${WORKTREE_DIR}" rev-parse --short HEAD)"
  # Plain commit subject of whatever is actually at the tip — used only for
  # the k8s deployment.change annotation and console output, never for a
  # GitHub URL/SHA cross-reference and never written into OTEL telemetry.
  COMMIT_SUBJECT="$(git -C "${WORKTREE_DIR}" log -1 --format=%s | tr -d '\n\r')"

  echo "    checked out origin/${BRANCH} @ ${COMMIT_SHA} into an isolated worktree"
  echo "    (your current branch/checkout in ${ROOT} is untouched)"
  echo ""
  echo "    About to deploy this commit — confirm this is the FIXED state"
  echo "    (i.e. you already merged/reverted the fix into ${BRANCH}):"
  echo "      ${COMMIT_SHA}  ${COMMIT_SUBJECT}"
  echo ""
}

build_and_push() {
  ensure_builder
  local metadata_file
  metadata_file="$(mktemp)"

  echo "==> Building productcatalogservice (linux/amd64, --no-cache) from origin/${BRANCH} @ ${COMMIT_SHA}..."
  docker buildx build \
    --builder "${BUILDER_NAME}" \
    --platform linux/amd64 \
    --no-cache --pull \
    -t "${ECR_URI}:productcatalogservice" \
    --metadata-file "${metadata_file}" \
    --push \
    "${WORKTREE_DIR}/src/productcatalogservice"

  DIGEST="$(digest_from_metadata "${metadata_file}")"
  rm -f "${metadata_file}"
  if [[ -z "${DIGEST}" ]]; then
    echo "ERROR: could not determine the pushed image digest from buildx metadata." >&2
    exit 1
  fi
  echo "    pushed digest: ${DIGEST}"
}

# Fail loudly if this run pushed the exact same image as the other role's
# last known-good run. This is the direct guard against the exact failure
# mode originally reported: a "fix" that silently re-pushes the broken
# binary because of a cached layer — and, now that both scripts build from
# the same branch, it's also your safety net against running this before
# the fix actually landed on feat/observe-otlp-apm.
check_distinct_digest() {
  local other_role="$1" other_digest=""
  mkdir -p "${STATE_DIR}"
  touch "${STATE_FILE}"
  # shellcheck disable=SC1090
  source "${STATE_FILE}"
  if [[ "${other_role}" == "fixed" ]]; then
    other_digest="${LAST_FIXED_DIGEST:-}"
  else
    other_digest="${LAST_BROKEN_DIGEST:-}"
  fi

  if [[ -n "${other_digest}" && "${DIGEST}" == "${other_digest}" ]]; then
    echo "" >&2
    echo "ERROR: the image just pushed for this run is byte-identical (digest" >&2
    echo "       ${DIGEST}) to the last known ${other_role^^} image." >&2
    echo "       That means the fix did NOT actually change ${BRANCH}'s tip" >&2
    echo "       — do not trust this rollout. Confirm the fix is really" >&2
    echo "       merged/reverted on ${BRANCH}, then re-run." >&2
    exit 1
  fi
}

save_digest() {
  local role="$1"
  mkdir -p "${STATE_DIR}"
  touch "${STATE_FILE}"
  # shellcheck disable=SC1090
  source "${STATE_FILE}"
  if [[ "${role}" == "broken" ]]; then
    LAST_BROKEN_DIGEST="${DIGEST}"
  else
    LAST_FIXED_DIGEST="${DIGEST}"
  fi
  {
    echo "LAST_BROKEN_DIGEST=${LAST_BROKEN_DIGEST:-}"
    echo "LAST_FIXED_DIGEST=${LAST_FIXED_DIGEST:-}"
  } > "${STATE_FILE}"
}

verify_running_digest() {
  local pod running_image_id=""
  pod="$(kubectl get pods -l app=productcatalogservice -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  for _ in $(seq 1 10); do
    if [[ -n "${pod}" ]]; then
      running_image_id="$(kubectl get pod "${pod}" -o jsonpath='{.status.containerStatuses[0].imageID}' 2>/dev/null || true)"
      [[ "${running_image_id}" == *"${DIGEST}"* ]] && break
    fi
    sleep 3
    pod="$(kubectl get pods -l app=productcatalogservice -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  done

  if [[ "${running_image_id}" != *"${DIGEST}"* ]]; then
    echo "ERROR: running productcatalogservice pod image (${running_image_id:-unknown})" >&2
    echo "       does not match the digest we just pushed (${DIGEST})." >&2
    echo "       The rollout may be serving a stale/cached image — do not" >&2
    echo "       start the demo yet." >&2
    exit 1
  fi
  echo "    confirmed: running pod is serving digest ${DIGEST}"
}

# ---------------------------------------------------------------------------

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${REPO_NAME}"

preflight
checkout_worktree

# Version only needs to be new so Observe fires a deployment marker — it is
# deliberately timestamp-based, not derived from the git SHA, so no commit
# identity leaks into OTEL_RESOURCE_ATTRIBUTES.
FIXED_VERSION="${DEMO_FIXED_PC_VERSION:-1.1.1-fixed-$(date -u +%Y%m%dT%H%M%SZ)}"

echo "==> Logging in to ECR..."
aws ecr get-login-password --region "${REGION}" | \
  docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com" >/dev/null

build_and_push
check_distinct_digest "broken"

echo "==> Rolling out fixed version (${FIXED_VERSION})..."

# --- productcatalogservice: new image + version bump (root cause fixed) ---
kubectl set image deployment/productcatalogservice \
  "server=${ECR_URI}:productcatalogservice"

kubectl annotate deployment/productcatalogservice \
  "deployment.version=${FIXED_VERSION}" \
  "deployment.change=${COMMIT_SUBJECT}" \
  --overwrite

kubectl set env deployment/productcatalogservice \
  "OTEL_RESOURCE_ATTRIBUTES=$(otel_resource_attributes "${FIXED_VERSION}")"

# --- checkoutservice + frontend: version bump to show recovery markers ---
for svc in checkoutservice frontend; do
  kubectl annotate "deployment/${svc}" \
    "deployment.version=${FIXED_VERSION}" \
    "deployment.change=release ${FIXED_VERSION}" \
    --overwrite
  kubectl set env "deployment/${svc}" \
    "OTEL_RESOURCE_ATTRIBUTES=$(otel_resource_attributes "${FIXED_VERSION}")"
done

echo "==> Waiting for rollouts..."
for svc in productcatalogservice checkoutservice frontend; do
  wait_ready "${svc}"
done

echo "==> Verifying running productcatalogservice pod matches the pushed image..."
verify_running_digest
save_digest "${ROLE}"

echo ""
echo "Service restored."
echo "  service.version=${FIXED_VERSION}"
echo "  image digest=${DIGEST}"
echo "  deployed commit: ${COMMIT_SHA}  ${COMMIT_SUBJECT}"
echo ""
echo "Correlate in Observe (no pre-baked link — join it live):"
echo "  1. APM → productcatalogservice → recovery deployment marker at"
echo "     service.version=${FIXED_VERSION}. Note the marker timestamp."
echo "  2. Kubernetes Explorer → productcatalogservice Deployment → find the"
echo "     rollout around that same timestamp (image changed again)."
echo "  3. GitHub → Commit dataset, filtered to ${BRANCH} → the nearest commit"
echo "     at or before that timestamp is the one that fixed it."
echo ""
echo "Verify: kubectl logs deployment/loadgenerator --tail=10"
