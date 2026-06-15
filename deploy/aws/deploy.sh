#!/usr/bin/env bash
# Bootstrap Online Boutique on a fresh AWS account: ECR + EKS + images + manifests.
#
# Usage:
#   ./deploy/aws/deploy.sh              # full deploy
#   ./deploy/aws/deploy.sh --skip-cluster   # reuse existing cluster
#   ./deploy/aws/deploy.sh --skip-build      # skip docker build/push

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
REGION="${AWS_REGION:-us-east-1}"
CLUSTER_NAME="${EKS_CLUSTER_NAME:-online-boutique}"
REPO_NAME="${ECR_REPO_NAME:-online-boutique}"
SKIP_CLUSTER=false
SKIP_BUILD=false

for arg in "$@"; do
  case "$arg" in
    --skip-cluster) SKIP_CLUSTER=true ;;
    --skip-build) SKIP_BUILD=true ;;
  esac
done

ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com/${REPO_NAME}"

echo "==> AWS account:  ${ACCOUNT_ID}"
echo "==> Region:       ${REGION}"
echo "==> ECR:          ${ECR_URI}"
echo "==> EKS cluster:  ${CLUSTER_NAME}"

echo "==> Ensuring ECR repository exists..."
aws ecr describe-repositories --repository-names "${REPO_NAME}" --region "${REGION}" >/dev/null 2>&1 || \
  aws ecr create-repository --repository-name "${REPO_NAME}" --region "${REGION}"

if [[ "${SKIP_CLUSTER}" == "false" ]]; then
  if eksctl get cluster --name "${CLUSTER_NAME}" --region "${REGION}" >/dev/null 2>&1; then
    echo "==> Cluster ${CLUSTER_NAME} already exists, updating kubeconfig..."
  else
    echo "==> Creating EKS cluster (this takes ~15 minutes)..."
    eksctl create cluster -f "${ROOT}/deploy/aws/cluster.yaml"
  fi
fi

echo "==> Configuring kubectl..."
aws eks update-kubeconfig --name "${CLUSTER_NAME}" --region "${REGION}"
kubectl cluster-info

if [[ "${SKIP_BUILD}" == "false" ]]; then
  echo "==> Logging in to ECR..."
  aws ecr get-login-password --region "${REGION}" | \
    docker login --username AWS --password-stdin "${ACCOUNT_ID}.dkr.ecr.${REGION}.amazonaws.com"

  IMAGES=(
    "adservice:src/adservice"
    "cartservice:src/cartservice/src"
    "checkoutservice:src/checkoutservice"
    "currencyservice:src/currencyservice"
    "emailservice:src/emailservice"
    "frontend:src/frontend"
    "loadgenerator:src/loadgenerator"
    "paymentservice:src/paymentservice"
    "productcatalogservice:src/productcatalogservice"
    "recommendationservice:src/recommendationservice"
    "shippingservice:src/shippingservice"
  )

  for entry in "${IMAGES[@]}"; do
    IMAGE="${entry%%:*}"
    CONTEXT="${entry#*:}"
    echo "==> Building and pushing ${IMAGE}..."
    docker buildx build --platform linux/amd64 \
      -t "${ECR_URI}:${IMAGE}" \
      --push "${ROOT}/${CONTEXT}"
  done
fi

echo "==> Applying Kubernetes manifests..."
KUSTOM_FILE="${ROOT}/deploy/aws/kustomization.yaml"
KUSTOM_BAK="${ROOT}/deploy/aws/kustomization.yaml.bak"
cp "${KUSTOM_FILE}" "${KUSTOM_BAK}"
sed -i '' "s|ECR_URI_PLACEHOLDER|${ECR_URI}|g" "${KUSTOM_FILE}"
kubectl apply -k "${ROOT}/deploy/aws"
mv "${KUSTOM_BAK}" "${KUSTOM_FILE}"

echo "==> Waiting for deployments..."
kubectl rollout status deployment/frontend --timeout=300s
kubectl rollout status deployment/checkoutservice --timeout=300s

echo "==> Waiting for LoadBalancer..."
for i in $(seq 1 60); do
  LB="$(kubectl get svc frontend-external -o jsonpath='{.status.loadBalancer.ingress[0].hostname}' 2>/dev/null || true)"
  if [[ -n "${LB}" ]]; then
    echo ""
    echo "=========================================="
    echo "Online Boutique is live:"
    echo "  http://${LB}/"
    echo "=========================================="
    exit 0
  fi
  sleep 10
done

echo "LoadBalancer not ready yet. Check with:"
echo "  kubectl get svc frontend-external"
