
#------------------------------------------------------------------#
# Build y push a ECR
#------------------------------------------------------------------#

aws sso login --profile innodia-admin

export AWS_PROFILE=innodia-admin
export AWS_REGION=eu-west-1
export ACCOUNT_ID=745854319016
export ECR_URI=${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com

export FRONTEND_REPO=cts-dashboard-frontend
export BACKEND_REPO=cts-dashboard-backend
export TAG=$(date +%Y%m%d-%H%M%S)

# Iniciar sesión en ECR
aws ecr get-login-password --region $AWS_REGION --profile $AWS_PROFILE | docker login --username AWS --password-stdin $ECR_URI

# Desde la raíz del repo (donde están backend/ y frontend/):
# A) BACKEND: build + push directo (recomendado)
docker buildx build \
  --platform linux/amd64 \
  -t "${ECR_URI}/${BACKEND_REPO}:${TAG}" \
  -t "${ECR_URI}/${BACKEND_REPO}:latest" \
  -f backend/Dockerfile.backend backend \
  --push

# Si buildx te pidiera crear builder:
# docker buildx create --use --name xbuilder || true
# docker buildx inspect --bootstrap

# B) FRONTEND: build + push directo (recomendado)
# Ajusta tu API base y la API key de Maps de producción
export VITE_API_BASE="https://cts-innodia-dashboard.org"
export VITE_GOOGLE_MAPS_API_KEY="AIzaSyBlizrJ8uQfzh4qYKiUPZ9xX-BL5ddzlO0"

docker buildx build \
  --platform linux/amd64 \
  --build-arg VITE_API_BASE="${VITE_API_BASE}" \
  --build-arg VITE_GOOGLE_MAPS_API_KEY="${VITE_GOOGLE_MAPS_API_KEY}" \
  -t "${ECR_URI}/${FRONTEND_REPO}:${TAG}" \
  -t "${ECR_URI}/${FRONTEND_REPO}:latest" \
  -f frontend/Dockerfile.frontend frontend \
  --push


#------------------------------------------------------------------#
# Desplegar nuevas imágenes en ECS
#------------------------------------------------------------------#

export CLUSTER=cts-dashboard
export SERVICE_FRONTEND=frontend
export TD_FAMILY_FRONTEND=cts-dashboard-frontend
export SERVICE_BACKEND=backend
export TD_FAMILY_BACKEND=cts-dashboard-backend

# FRONTEND
aws ecs describe-task-definition \
  --profile innodia-admin \
  --task-definition "$TD_FAMILY_FRONTEND" \
  --query 'taskDefinition' \
  --output json > td-frontend.json

jq 'del(.taskDefinitionArn,.revision,.status,.requiresAttributes,.compatibilities,.registeredBy,.registeredAt)' \
  td-frontend.json > td-frontend-editable.json

jq --arg img "${ECR_URI}/${FRONTEND_REPO}:${TAG}" \
  '.containerDefinitions[0].image = $img' \
  td-frontend-editable.json > td-frontend-new.json

NEW_TD_FRONTEND_ARN=$(aws ecs register-task-definition \
  --profile innodia-admin \
  --cli-input-json file://td-frontend-new.json \
  --query 'taskDefinition.taskDefinitionArn' \
  --output text)

aws ecs update-service \
  --profile innodia-admin \
  --cluster "$CLUSTER" \
  --service "$SERVICE_FRONTEND" \
  --task-definition "$NEW_TD_FRONTEND_ARN" \
  --force-new-deployment

aws ecs wait services-stable --profile innodia-admin --cluster "$CLUSTER" --services "$SERVICE_FRONTEND"

# BACKEND
aws ecs describe-task-definition \
  --profile innodia-admin \
  --task-definition "$TD_FAMILY_BACKEND" \
  --query 'taskDefinition' \
  --output json > td-backend.json

jq 'del(.taskDefinitionArn,.revision,.status,.requiresAttributes,.compatibilities,.registeredBy,.registeredAt)' \
  td-backend.json > td-backend-editable.json

jq --arg img "${ECR_URI}/${BACKEND_REPO}:${TAG}" \
  '.containerDefinitions[0].image = $img' \
  td-backend-editable.json > td-backend-new.json

NEW_TD_BACKEND_ARN=$(aws ecs register-task-definition \
  --profile innodia-admin \
  --cli-input-json file://td-backend-new.json \
  --query 'taskDefinition.taskDefinitionArn' \
  --output text)

aws ecs update-service \
  --profile innodia-admin \
  --cluster "$CLUSTER" \
  --service "$SERVICE_BACKEND" \
  --task-definition "$NEW_TD_BACKEND_ARN" \
  --force-new-deployment

aws ecs wait services-stable --profile innodia-admin --cluster "$CLUSTER" --services "$SERVICE_BACKEND"

