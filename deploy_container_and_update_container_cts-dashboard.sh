# Lanzar cts-dashboard_v3 en local

unset VITE_API_BASE
docker compose -f docker-compose.yml down -v
docker compose -f docker-compose.yml build --no-cache
docker compose -f docker-compose.yml up -d
docker compose -f docker-compose.yml ps


docker compose down -v
rm -rf frontend/dist
docker compose build --no-cache
docker compose up -d



# 0) Ver nombres reales de servicios
docker compose ps

# 1) Parar sólo el frontend (opcional)
docker compose stop frontend

# 2) Recompilar el contenedor del frontend
docker compose build --no-cache frontend

# 3) Levantarlo en segundo plano
docker compose up -d frontend

# 4) Ver logs en vivo del frontend
docker compose logs -f frontend


# Ver logs del backend (para chequear healthz y Salesforce)
docker compose -f docker-compose.yml logs -f backend

# Ver logs del frontend
docker compose -f docker-compose.yml logs -f frontend

# En caso de querer reiniciar
docker compose -f docker-compose.yml down -v


# Deployment a AWS
aws sso login --profile innodia-admin

export AWS_PROFILE=innodia-admin
export AWS_REGION=eu-west-1
export ACCOUNT_ID=745854319016
export ECR_URI=${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com

# Nombres de repos ECR que ya tienes
export BACKEND_REPO=cts-dashboard-backend
export FRONTEND_REPO=cts-dashboard-frontend

# Tags (uno con timestamp + "latest")
export TAG=$(date +%Y%m%d-%H%M%S)

aws ecr get-login-password --profile "$AWS_PROFILE" --region "$AWS_REGION" \
| docker login --username AWS --password-stdin "$ECR_URI"

# Desde la raíz del repo (donde están backend/ y frontend/):
# A) BACKEND: build + push directo (recomendado)
docker buildx build \
  --platform linux/amd64 \
  -t "${ECR_URI}/${BACKEND_REPO}:${TAG}" \
  -t "${ECR_URI}/${BACKEND_REPO}:latest" \
  -f backend/Dockerfile.backend backend \
  --push

# Si buildx te pidiera crear builder:
docker buildx create --use --name xbuilder || true
docker buildx inspect --bootstrap

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
# Forzar nuevo despliegue en ECS (backend y frontend)
#------------------------------------------------------------------#

export AWS_PROFILE=innodia-admin
export AWS_REGION=eu-west-1
export CLUSTER=cts-dashboard

export ACCOUNT_ID=745854319016
export ECR_URI=${ACCOUNT_ID}.dkr.ecr.${AWS_REGION}.amazonaws.com

export FRONTEND_REPO=cts-dashboard-frontend
export BACKEND_REPO=cts-dashboard-backend
# Usa el mismo TAG que usaste al hacer --push
#export TAG=YYYYMMDD-HHMMSS

# nombres de service y families
export SERVICE_FRONTEND=frontend
export TD_FAMILY_FRONTEND=cts-dashboard-frontend

# Descarga el task definition actual, cambia la imagen y registra una revisión nueva:
# 1) Obtener TD actual
aws ecs describe-task-definition \
  --task-definition "$TD_FAMILY_FRONTEND" \
  --query 'taskDefinition' \
  --output json > td-frontend.json

# 2) Quitar campos de solo-lectura para poder registrar
jq 'del(.taskDefinitionArn,.revision,.status,.requiresAttributes,.compatibilities,.registeredBy,.registeredAt)' \
  td-frontend.json > td-frontend-editable.json

# 3) Reemplazar la imagen del primer contenedor (ajusta índice si tu TD tiene varios)
jq --arg img "${ECR_URI}/${FRONTEND_REPO}:${TAG}" \
  '.containerDefinitions[0].image = $img' \
  td-frontend-editable.json > td-frontend-new.json

# 4) Registrar la nueva revisión
NEW_TD_FRONTEND_ARN=$(aws ecs register-task-definition \
  --cli-input-json file://td-frontend-new.json \
  --query 'taskDefinition.taskDefinitionArn' \
  --output text)
echo "Nueva TD frontend: $NEW_TD_FRONTEND_ARN" #arn:aws:ecs:eu-west-1:745854319016:task-definition/cts-dashboard-frontend:4

#  Actualiza el servicio frontend para usar la nueva revisión
aws ecs update-service \
  --cluster "$CLUSTER" \
  --service "$SERVICE_FRONTEND" \
  --task-definition "$NEW_TD_FRONTEND_ARN" \
  --force-new-deployment

# Verifica que el despliegue complete
aws ecs describe-services \
  --cluster "$CLUSTER" \
  --services "$SERVICE_FRONTEND" \
  --query 'services[0].deployments'

aws ecs wait services-stable \
  --cluster "$CLUSTER" \
  --services "$SERVICE_FRONTEND"


export BACKEND_REPO=cts-dashboard-backend
# Usa el mismo TAG que usaste al hacer el push de la imagen del backend:
# export TAG=YYYYMMDD-HHMMSS

# Nombres de service y task family del backend en ECS
export SERVICE_BACKEND=backend
export TD_FAMILY_BACKEND=cts-dashboard-backend

# 1) Bajar la Task Definition actual del backend
aws ecs describe-task-definition \
  --task-definition "$TD_FAMILY_BACKEND" \
  --query 'taskDefinition' \
  --output json > td-backend.json

# 2) Limpiar campos de solo-lectura
jq 'del(.taskDefinitionArn,.revision,.status,.requiresAttributes,.compatibilities,.registeredBy,.registeredAt)' \
  td-backend.json > td-backend-editable.json

# 3) Reemplazar la imagen del contenedor
# export CONTAINER_NAME_BACKEND=backend
jq --arg name "${CONTAINER_NAME_BACKEND:-backend}" --arg img "${ECR_URI}/${BACKEND_REPO}:${TAG}" '
  .containerDefinitions = (.containerDefinitions | map(
    if .name == $name then .image = $img | . else . end
  ))
' td-backend-editable.json > td-backend-new.json

# 4) Registrar la nueva revisión de la Task Definition
NEW_TD_BACKEND_ARN=$(aws ecs register-task-definition \
  --cli-input-json file://td-backend-new.json \
  --query 'taskDefinition.taskDefinitionArn' \
  --output text)

echo "Nueva TD backend: $NEW_TD_BACKEND_ARN"
  
# 5) Actualizar el servicio para usar la nueva revisión y forzar despliegue
aws ecs update-service \
  --cluster "$CLUSTER" \
  --service "$SERVICE_BACKEND" \
  --task-definition "$NEW_TD_BACKEND_ARN" \
  --force-new-deployment

# 6) (Opcional) Ver y esperar a que quede estable
aws ecs describe-services \
  --cluster "$CLUSTER" \
  --services "$SERVICE_BACKEND" \
  --query 'services[0].deployments'

aws ecs wait services-stable \
  --cluster "$CLUSTER" \
  --services "$SERVICE_BACKEND"


# Forzar new deployment con tag vieja
export AWS_PROFILE=innodia-admin
export AWS_REGION=eu-west-1
export ECR_URI=745854319016.dkr.ecr.${AWS_REGION}.amazonaws.com
export TAG=20250924-113102   # el tag que da error

aws ecr get-login-password --region "$AWS_REGION" \
| docker login --username AWS --password-stdin "$ECR_URI"

# (Re)build & push
docker buildx build \
  --platform linux/amd64 \
  --build-arg VITE_API_BASE="https://cts-innodia-dashboard.org" \
  --build-arg VITE_GOOGLE_MAPS_API_KEY="$VITE_GOOGLE_MAPS_API_KEY" \
  -t "${ECR_URI}/cts-dashboard-frontend:${TAG}" \
  -t "${ECR_URI}/cts-dashboard-frontend:latest" \
  -f frontend/Dockerfile.frontend frontend \
  --push

aws ecs update-service \
  --cluster cts-dashboard \
  --service backend \
  --task-definition cts-dashboard-backend:4 \
  --force-new-deployment

aws ecs wait services-stable --cluster cts-dashboard --services backend

