#!/usr/bin/env bash
set -euo pipefail

# ─── Load deploy config ───────────────────────────────────────────────────── #
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=.env.deploy
source "${SCRIPT_DIR}/.env.deploy"

# ─── Derived vars ─────────────────────────────────────────────────────────── #
export AWS_PROFILE
export AWS_REGION

# Ensure AWS CLI v2 and Docker Desktop binaries take precedence
export PATH="/usr/local/bin:/opt/homebrew/bin:/Applications/Docker.app/Contents/Resources/bin:$PATH"
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
TAG=$(date +%Y%m%d-%H%M%S)

# ─── Helpers ──────────────────────────────────────────────────────────────── #
log() { printf "\n\033[1;34m==> %s\033[0m\n" "$*"; }
ok()  { printf "\033[1;32m✓  %s\033[0m\n"   "$*"; }
err() { printf "\033[1;31m✗  %s\033[0m\n"   "$*" >&2; }

cleanup() {
  rm -f td-frontend.json td-frontend-editable.json td-frontend-new.json \
        td-backend.json  td-backend-editable.json  td-backend-new.json
}
trap cleanup EXIT

req() { command -v "$1" >/dev/null 2>&1 || { err "Missing '$1' in PATH"; exit 1; }; }
req aws; req jq; req docker

# ─── AWS SSO ──────────────────────────────────────────────────────────────── #
log "AWS SSO login (profile: $AWS_PROFILE)"
aws sso login --profile "$AWS_PROFILE"

# ─── ECR login ────────────────────────────────────────────────────────────── #
log "Login to ECR"
aws ecr get-login-password --region "$AWS_REGION" --profile "$AWS_PROFILE" | \
  docker login --username AWS --password-stdin "$ECR_URI"

# ─── buildx builder ───────────────────────────────────────────────────────── #
docker buildx inspect xbuilder >/dev/null 2>&1 || docker buildx create --use --name xbuilder

# ─── Build & push BACKEND ─────────────────────────────────────────────────── #
log "Build & push BACKEND  →  ${BACKEND_REPO}:${TAG}"
docker buildx build \
  --platform linux/amd64 \
  -t "${ECR_URI}/${BACKEND_REPO}:${TAG}" \
  -t "${ECR_URI}/${BACKEND_REPO}:latest" \
  -f backend/Dockerfile.backend backend \
  --push
ok "Backend pushed"

# ─── Build & push FRONTEND ────────────────────────────────────────────────── #
log "Build & push FRONTEND  →  ${FRONTEND_REPO}:${TAG}"
docker buildx build \
  --platform linux/amd64 \
  --build-arg VITE_API_BASE="${VITE_API_BASE}" \
  --build-arg VITE_GOOGLE_MAPS_API_KEY="${VITE_GOOGLE_MAPS_API_KEY}" \
  -t "${ECR_URI}/${FRONTEND_REPO}:${TAG}" \
  -t "${ECR_URI}/${FRONTEND_REPO}:latest" \
  -f frontend/Dockerfile.frontend frontend \
  --push
ok "Frontend pushed"

# ─── Roll FRONTEND ────────────────────────────────────────────────────────── #
log "Register new FRONTEND task definition"
aws ecs describe-task-definition \
  --profile "$AWS_PROFILE" \
  --task-definition "$TD_FAMILY_FRONTEND" \
  --query 'taskDefinition' --output json > td-frontend.json

jq 'del(.taskDefinitionArn,.revision,.status,.requiresAttributes,.compatibilities,.registeredBy,.registeredAt)' \
  td-frontend.json > td-frontend-editable.json

jq --arg img "${ECR_URI}/${FRONTEND_REPO}:${TAG}" \
  '.containerDefinitions[0].image = $img' \
  td-frontend-editable.json > td-frontend-new.json

NEW_TD_FRONTEND_ARN=$(aws ecs register-task-definition \
  --profile "$AWS_PROFILE" \
  --cli-input-json file://td-frontend-new.json \
  --query 'taskDefinition.taskDefinitionArn' --output text)

log "Update FRONTEND service"
aws ecs update-service \
  --profile "$AWS_PROFILE" \
  --cluster "$CLUSTER" --service "$SERVICE_FRONTEND" \
  --task-definition "$NEW_TD_FRONTEND_ARN" --force-new-deployment

log "Waiting for FRONTEND to stabilize..."
aws ecs wait services-stable \
  --profile "$AWS_PROFILE" \
  --cluster "$CLUSTER" --services "$SERVICE_FRONTEND"
ok "Frontend stable"

# ─── Roll BACKEND ─────────────────────────────────────────────────────────── #
log "Register new BACKEND task definition"
aws ecs describe-task-definition \
  --profile "$AWS_PROFILE" \
  --task-definition "$TD_FAMILY_BACKEND" \
  --query 'taskDefinition' --output json > td-backend.json

jq 'del(.taskDefinitionArn,.revision,.status,.requiresAttributes,.compatibilities,.registeredBy,.registeredAt)' \
  td-backend.json > td-backend-editable.json

jq --arg img "${ECR_URI}/${BACKEND_REPO}:${TAG}" \
  '.containerDefinitions[0].image = $img' \
  td-backend-editable.json > td-backend-new.json

NEW_TD_BACKEND_ARN=$(aws ecs register-task-definition \
  --profile "$AWS_PROFILE" \
  --cli-input-json file://td-backend-new.json \
  --query 'taskDefinition.taskDefinitionArn' --output text)

log "Update BACKEND service"
aws ecs update-service \
  --profile "$AWS_PROFILE" \
  --cluster "$CLUSTER" --service "$SERVICE_BACKEND" \
  --task-definition "$NEW_TD_BACKEND_ARN" --force-new-deployment

log "Waiting for BACKEND to stabilize..."
aws ecs wait services-stable \
  --profile "$AWS_PROFILE" \
  --cluster "$CLUSTER" --services "$SERVICE_BACKEND"
ok "Backend stable"

# ─── Alembic migration ────────────────────────────────────────────────────── #
log "Run Alembic migration (one-off Fargate task)"
TASK_ARN=$(aws ecs run-task \
  --profile "$AWS_PROFILE" \
  --cluster "$CLUSTER" \
  --launch-type FARGATE \
  --task-definition "$NEW_TD_BACKEND_ARN" \
  --network-configuration "awsvpcConfiguration={subnets=[${SUBNET1},${SUBNET2}],securityGroups=[${SG}],assignPublicIp=${ASSIGN_PUBLIC_IP}}" \
  --overrides "{
    \"containerOverrides\":[{
      \"name\":\"backend\",
      \"command\":[\"alembic\",\"upgrade\",\"head\"],
      \"environment\":[{\"name\":\"DATABASE_URL\",\"value\":\"${RDS_DATABASE_URL}\"}]
    }]
  }" \
  --query 'tasks[0].taskArn' --output text)

log "Migration task started: $TASK_ARN"
log "Waiting for migration to finish..."
aws ecs wait tasks-stopped \
  --profile "$AWS_PROFILE" \
  --cluster "$CLUSTER" \
  --tasks "$TASK_ARN"

EXIT_CODE=$(aws ecs describe-tasks \
  --profile "$AWS_PROFILE" \
  --cluster "$CLUSTER" \
  --tasks "$TASK_ARN" \
  --query 'tasks[0].containers[0].exitCode' --output text)

if [ "$EXIT_CODE" != "0" ]; then
  err "Migration failed with exit code $EXIT_CODE"
  exit 1
fi
ok "Migration completed"

# ─── Done ─────────────────────────────────────────────────────────────────── #
printf "\n\033[1;32m════════════════════════════════════════\033[0m\n"
printf "\033[1;32m  Deploy complete!  Tag: %s\033[0m\n" "$TAG"
printf "\033[1;32m════════════════════════════════════════\033[0m\n\n"
