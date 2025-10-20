# 1. Autenticarse en ECR
aws ecr get-login-password --region eu-west-1 | docker login --username AWS --password-stdin 745854319016.dkr.ecr.eu-west-1.amazonaws.com

# 2. Construir la imagen (desde tu backend)
docker build -t cts-backend .

# 3. Taggear la imagen con la URI de ECR
docker tag cts-backend:latest 745854319016.dkr.ecr.eu-west-1.amazonaws.com/cts-backend:latest

# 4. Subir la imagen a ECR
docker push 745854319016.dkr.ecr.eu-west-1.amazonaws.com/cts-backend:latest