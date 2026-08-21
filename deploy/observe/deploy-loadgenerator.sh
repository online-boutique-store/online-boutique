#!/usr/bin/env bash
# Build, push, and deploy the Locust load generator against the in-cluster frontend.
#
# Usage:
#   ./deploy/observe/deploy-loadgenerator.sh
#   USERS=20 SPAWN_RATE=3 ./deploy/observe/deploy-loadgenerator.sh

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REGION="${AWS_REGION:-us-east-1}"
REPO_NAME="${ECR_REPO_NAME:-online-boutique}"
USERS="${USERS:-20}"
SPAWN_RATE="${SPAWN_RATE:-3}"

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${REPO_NAME}"

echo "==> Building loadgenerator..."
aws ecr describe-repositories --repository-names "${REPO_NAME}" --region "${REGION}" >/dev/null 2>&1 || \
  aws ecr create-repository --repository-name "${REPO_NAME}" --region "${REGION}"

aws ecr get-login-password --region "${REGION}" | \
  docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

docker buildx build --platform linux/amd64 \
  -t "${ECR_URI}:loadgenerator" \
  --push "${ROOT}/src/loadgenerator"

echo "==> Deploying loadgenerator (${USERS} users, spawn rate ${SPAWN_RATE}/s)..."
sed -e "s|IMAGE_PLACEHOLDER|${ECR_URI}:loadgenerator|g" \
    -e "s|USERS_PLACEHOLDER|${USERS}|g" \
    -e "s|SPAWN_RATE_PLACEHOLDER|${SPAWN_RATE}|g" \
  "${ROOT}/deploy/observe/loadgenerator.yaml" | kubectl apply -f -

kubectl rollout status deployment/loadgenerator --timeout=180s

echo ""
echo "Load generator is running. Tail traffic stats:"
echo "  kubectl logs -f deployment/loadgenerator"
echo ""
echo "Stop it:"
echo "  kubectl delete deployment loadgenerator"
