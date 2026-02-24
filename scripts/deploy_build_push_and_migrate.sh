#!/usr/bin/env bash
set -euo pipefail

# Deploy backend/frontend images to ECR, roll ECS services and run Alembic migration as a one-off task.
# Prereqs: awscli v2, jq, docker buildx, AWS SSO login (or valid creds), ECR repos exist.
# Usage: ./scripts/deploy_build_push_and_migrate.sh

# ---------------------------- Config ---------------------------- #
: "${AWS_PROFILE:=innodia-admin}"
: "${AWS_REGION:=eu-west-1}"
: "${ACCOUNT_ID:=745854319016}"
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"

# Images/repos
: "${FRONTEND_REPO:=cts-dashboard-frontend}"
: "${BACKEND_REPO:=cts-dashboard-backend}"
: "${TAG:=$(date +%Y%m%d-%H%M%S)}"

# ECS
: "${CLUSTER:=cts-dashboard}"
: "${SERVICE_FRONTEND:=frontend}"
: "${TD_FAMILY_FRONTEND:=cts-dashboard-frontend}"
: "${SERVICE_BACKEND:=backend}"
: "${TD_FAMILY_BACKEND:=cts-dashboard-backend}"

# Network for the one-off migration task (Fargate)
: "${SUBNET1:?Set SUBNET1}"
: "${SUBNET2:?Set SUBNET2}"
: "${SG:?Set SG}"

# Database URL for Alembic (safe to pass as env var)
: "${RDS_DATABASE_URL:?Set RDS_DATABASE_URL (postgresql+psycopg://...)}"

# Optional frontend build-time args
: "${VITE_API_BASE:=https://cts-innodia-dashboard.org}"
: "${VITE_GOOGLE_MAPS_API_KEY:=}"

export AWS_PROFILE AWS_REGION

# ---------------------------- Helpers ---------------------------- #
req() { command -v "$1" >/dev/null 2>&1 || { echo "Missing '$1' in PATH" >&2; exit 1; }; }
req aws; req jq; req docker

log() { printf "\n==> %s\n" "$*"; }

# ---------------------------- Login ECR -------------------------- #
log "AWS SSO (if needed)"
aws sso login --profile "$AWS_PROFILE" || true

log "Login to ECR"
aws ecr get-login-password --region "$AWS_REGION" --profile "$AWS_PROFILE" | \
  docker login --username AWS --password-stdin "$ECR_URI"

# ---------------------------- Build & Push ----------------------- #
log "Build & push BACKEND image: $TAG"
# ensure buildx builder exists
(docker buildx inspect xbuilder >/dev/null 2>&1) || docker buildx create --use --name xbuilder

docker buildx build \
  --platform linux/amd64 \
  -t "${ECR_URI}/${BACKEND_REPO}:${TAG}" \
  -t "${ECR_URI}/${BACKEND_REPO}:latest" \
  -f backend/Dockerfile.backend backend \
  --push

log "Build & push FRONTEND image: $TAG"
docker buildx build \
  --platform linux/amd64 \
  --build-arg VITE_API_BASE="$VITE_API_BASE" \
  ${VITE_GOOGLE_MAPS_API_KEY:+--build-arg VITE_GOOGLE_MAPS_API_KEY="$VITE_GOOGLE_MAPS_API_KEY"} \
  -t "${ECR_URI}/${FRONTEND_REPO}:${TAG}" \
  -t "${ECR_URI}/${FRONTEND_REPO}:latest" \
  -f frontend/Dockerfile.frontend frontend \
  --push

# ---------------------------- Roll ECS FRONTEND ------------------ #
log "Register new FRONTEND task definition"
aws ecs describe-task-definition \
  --task-definition "$TD_FAMILY_FRONTEND" \
  --query 'taskDefinition' --output json > td-frontend.json
jq 'del(.taskDefinitionArn,.revision,.status,.requiresAttributes,.compatibilities,.registeredBy,.registeredAt)' \
  td-frontend.json > td-frontend-editable.json
jq --arg img "${ECR_URI}/${FRONTEND_REPO}:${TAG}" \
  '.containerDefinitions[0].image = $img' \
  td-frontend-editable.json > td-frontend-new.json
NEW_TD_FRONTEND_ARN=$(aws ecs register-task-definition --cli-input-json file://td-frontend-new.json \
  --query 'taskDefinition.taskDefinitionArn' --output text)

log "Update FRONTEND service"
aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE_FRONTEND" \
  --task-definition "$NEW_TD_FRONTEND_ARN" --force-new-deployment
aws ecs wait services-stable --cluster "$CLUSTER" --services "$SERVICE_FRONTEND"

# ---------------------------- Roll ECS BACKEND ------------------- #
log "Register new BACKEND task definition"
aws ecs describe-task-definition \
  --task-definition "$TD_FAMILY_BACKEND" \
  --query 'taskDefinition' --output json > td-backend.json
jq 'del(.taskDefinitionArn,.revision,.status,.requiresAttributes,.compatibilities,.registeredBy,.registeredAt)' \
  td-backend.json > td-backend-editable.json
jq --arg img "${ECR_URI}/${BACKEND_REPO}:${TAG}" \
  '.containerDefinitions[0].image = $img' \
  td-backend-editable.json > td-backend-new.json
NEW_TD_BACKEND_ARN=$(aws ecs register-task-definition --cli-input-json file://td-backend-new.json \
  --query 'taskDefinition.taskDefinitionArn' --output text)

log "Update BACKEND service"
aws ecs update-service --cluster "$CLUSTER" --service "$SERVICE_BACKEND" \
  --task-definition "$NEW_TD_BACKEND_ARN" --force-new-deployment
aws ecs wait services-stable --cluster "$CLUSTER" --services "$SERVICE_BACKEND"

# ---------------------------- Alembic migration ------------------ #
log "Run Alembic migration one-off task"
RUN_TASK_JSON=$(aws ecs run-task \
  --cluster "$CLUSTER" \
  --launch-type FARGATE \
  --task-definition "$NEW_TD_BACKEND_ARN" \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNET1,$SUBNET2],securityGroups=[$SG],assignPublicIp=DISABLED}" \
  --overrides '{
    "containerOverrides":[{
      "name":"backend",
      "command":["alembic","upgrade","head"],
      "environment":[{"name":"DATABASE_URL","value":"'