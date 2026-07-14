#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# deploy.sh — Build + push Docker images to ECR and roll ECS services
#
# Usage:
#   bash scripts/deploy.sh                  # deploy backend + frontend
#   bash scripts/deploy.sh --backend-only   # backend only
#   bash scripts/deploy.sh --frontend-only  # frontend only
#   bash scripts/deploy.sh --migrate        # also run Alembic migration task
#
# Requirements:
#   - Must be on 'main' branch (enforced)
#   - backend/.env must exist (run gen_local_env.py if missing)
#   - aws sso login --profile juan (if SSO token expired)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
BACKEND_DIR="$REPO_ROOT/backend"

# ── Guard: must be on main ────────────────────────────────────────────────────
CURRENT_BRANCH=$(git -C "$REPO_ROOT" rev-parse --abbrev-ref HEAD)
if [[ "$CURRENT_BRANCH" != "main" ]]; then
  echo "❌  You are on branch '$CURRENT_BRANCH', not 'main'."
  echo "    Merge your changes first:"
  echo "      git checkout main"
  echo "      git merge dev"
  echo "      bash scripts/deploy.sh"
  exit 1
fi

# ── Parse flags ───────────────────────────────────────────────────────────────
DEPLOY_BACKEND=true
DEPLOY_FRONTEND=true
RUN_MIGRATE=false

for arg in "$@"; do
  case $arg in
    --backend-only)  DEPLOY_FRONTEND=false ;;
    --frontend-only) DEPLOY_BACKEND=false  ;;
    --migrate)       RUN_MIGRATE=true      ;;
  esac
done

# ── Config ────────────────────────────────────────────────────────────────────
AWS_PROFILE="juan"
AWS_REGION="eu-west-1"
ACCOUNT_ID="745854319016"
ECR_URI="${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com"
CLUSTER="cts-dashboard"
TAG=$(date +%Y%m%d-%H%M%S)

# Read frontend API key from backend/.env
if [ ! -f "$BACKEND_DIR/.env" ]; then
  echo "❌  backend/.env not found. Run: python3 scripts/gen_local_env.py"
  exit 1
fi
VITE_GOOGLE_MAPS_API_KEY=$(grep '^GOOGLE_MAPS_API_KEY=' "$BACKEND_DIR/.env" | head -1 | cut -d= -f2 | tr -d "\"'" || echo "")

log() { printf "\n\033[1;34m==> %s\033[0m\n" "$*" >&2; }
ok()  { printf "\033[1;32m✓  %s\033[0m\n" "$*" >&2; }

# ── ECR login ─────────────────────────────────────────────────────────────────
log "ECR login"
aws ecr get-login-password --region "$AWS_REGION" --profile "$AWS_PROFILE" | \
  docker login --username AWS --password-stdin "$ECR_URI"
ok "ECR authenticated"

# ── Ensure buildx builder ──────────────────────────────────────────────────────
(docker buildx inspect xbuilder >/dev/null 2>&1) || \
  docker buildx create --use --name xbuilder >/dev/null

# ── Helper: roll ECS service ───────────────────────────────────────────────────
roll_service() {
  local repo=$1 family=$2 service=$3 image="${ECR_URI}/${1}:${TAG}"

  log "Register task definition: $family"
  aws ecs describe-task-definition \
    --task-definition "$family" --query 'taskDefinition' \
    --output json --region "$AWS_REGION" --profile "$AWS_PROFILE" \
    > /tmp/td_${service}.json
  jq 'del(.taskDefinitionArn,.revision,.status,.requiresAttributes,.compatibilities,.registeredBy,.registeredAt)' \
    /tmp/td_${service}.json \
  | jq --arg img "$image" '.containerDefinitions[0].image = $img' \
    > /tmp/td_${service}_new.json
  NEW_ARN=$(aws ecs register-task-definition \
    --cli-input-json "file:///tmp/td_${service}_new.json" \
    --query 'taskDefinition.taskDefinitionArn' --output text \
    --region "$AWS_REGION" --profile "$AWS_PROFILE")

  log "Update ECS service: $service"
  aws ecs update-service \
    --cluster "$CLUSTER" --service "$service" \
    --task-definition "$NEW_ARN" --force-new-deployment \
    --region "$AWS_REGION" --profile "$AWS_PROFILE" > /dev/null
  aws ecs wait services-stable \
    --cluster "$CLUSTER" --services "$service" \
    --region "$AWS_REGION" --profile "$AWS_PROFILE"

  # "stable" NO significa "desplegado". Si la task nueva no arranca, ECS revierte
  # sola a la anterior y el servicio vuelve a estar estable — con el código VIEJO.
  # `wait services-stable` devuelve éxito igual, así que sin esta comprobación el
  # script cantaba "✅ Deploy complete" sobre un deploy fallido (pasó el 2026-07-14).
  local LIVE_ARN
  LIVE_ARN=$(aws ecs describe-services \
    --cluster "$CLUSTER" --services "$service" \
    --query "services[0].deployments[?status=='PRIMARY'].taskDefinition | [0]" \
    --output text --region "$AWS_REGION" --profile "$AWS_PROFILE")

  if [ "$LIVE_ARN" != "$NEW_ARN" ]; then
    echo "❌  $service NO se desplegó: ECS revirtió a ${LIVE_ARN##*/} (esperada ${NEW_ARN##*/})." >&2
    echo "    La task nueva no arrancó. Logs:" >&2
    echo "    aws logs tail /ecs/$service --since 15m --profile $AWS_PROFILE --region $AWS_REGION" >&2
    exit 1
  fi

  ok "$service stable → $NEW_ARN"
  echo "$NEW_ARN"  # return value
}

# ── Backend ───────────────────────────────────────────────────────────────────
if $DEPLOY_BACKEND; then
  log "Build & push backend: $TAG"
  docker buildx build \
    --platform linux/amd64 \
    -t "${ECR_URI}/cts-dashboard-backend:${TAG}" \
    -t "${ECR_URI}/cts-dashboard-backend:latest" \
    -f "$REPO_ROOT/backend/Dockerfile.backend" "$REPO_ROOT/backend" \
    --push
  ok "Backend image pushed"

  BACKEND_ARN=$(roll_service cts-dashboard-backend cts-dashboard-backend backend)
fi

# ── Frontend ──────────────────────────────────────────────────────────────────
if $DEPLOY_FRONTEND; then
  log "Build & push frontend: $TAG"
  docker buildx build \
    --platform linux/amd64 \
    ${VITE_GOOGLE_MAPS_API_KEY:+--build-arg VITE_GOOGLE_MAPS_API_KEY="$VITE_GOOGLE_MAPS_API_KEY"} \
    -t "${ECR_URI}/cts-dashboard-frontend:${TAG}" \
    -t "${ECR_URI}/cts-dashboard-frontend:latest" \
    -f "$REPO_ROOT/frontend/Dockerfile.frontend" "$REPO_ROOT/frontend" \
    --push
  ok "Frontend image pushed"

  roll_service cts-dashboard-frontend cts-dashboard-frontend frontend > /dev/null
fi

# ── Alembic migration (optional) ──────────────────────────────────────────────
if $RUN_MIGRATE; then
  if [ -z "${BACKEND_ARN:-}" ]; then
    BACKEND_ARN=$(aws ecs describe-task-definition \
      --task-definition cts-dashboard-backend \
      --query 'taskDefinition.taskDefinitionArn' --output text \
      --region "$AWS_REGION" --profile "$AWS_PROFILE")
  fi
  SUBNET1="subnet-0e345d0d0c88c38ac"
  SUBNET2="subnet-0966b624cffd8a716"
  SG="sg-039384ad98327e408"
  DATABASE_URL=$(grep '^DATABASE_URL=' "$BACKEND_DIR/.env" | head -1 | cut -d= -f2)

  log "Run Alembic migration"
  TASK_ARN=$(aws ecs run-task \
    --cluster "$CLUSTER" --launch-type FARGATE \
    --task-definition "$BACKEND_ARN" \
    --network-configuration "awsvpcConfiguration={subnets=[$SUBNET1,$SUBNET2],securityGroups=[$SG],assignPublicIp=ENABLED}" \
    --overrides "{\"containerOverrides\":[{\"name\":\"backend\",\"command\":[\"alembic\",\"upgrade\",\"head\"],\"environment\":[{\"name\":\"DATABASE_URL\",\"value\":\"$DATABASE_URL\"}]}]}" \
    --query 'tasks[0].taskArn' --output text \
    --region "$AWS_REGION" --profile "$AWS_PROFILE")
  echo "  Migration task: $TASK_ARN"
  aws ecs wait tasks-stopped --cluster "$CLUSTER" --tasks "$TASK_ARN" \
    --region "$AWS_REGION" --profile "$AWS_PROFILE"
  EXIT_CODE=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK_ARN" \
    --query 'tasks[0].containers[0].exitCode' --output text \
    --region "$AWS_REGION" --profile "$AWS_PROFILE")
  if [[ "$EXIT_CODE" == "0" ]]; then
    ok "Migration completed successfully"
  else
    echo "❌  Migration failed (exit $EXIT_CODE)"
    exit 1
  fi
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "✅  Deploy complete — tag: $TAG"
echo "    https://cts-innodia-dashboard.org"
