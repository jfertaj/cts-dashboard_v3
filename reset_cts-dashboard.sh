#!/bin/bash
echo "🧨 Stopping containers..."
docker compose down

echo "🧹 Removing volumes..."
docker volume rm $(docker volume ls -q | grep cts-dashboard)

echo "🔨 Rebuilding containers..."
docker compose build

echo "🚀 Starting services..."
docker compose up -d

#echo "⏱️ Waiting for database to be ready..."
#sleep 5

echo "⏱️ Waiting for PostgreSQL to be ready..."
until docker compose exec db pg_isready -U ctsuser > /dev/null 2>&1; do
  echo "⏳ Waiting for PostgreSQL..."
  sleep 2
done

echo "✅ PostgreSQL is ready!"

#echo "📦 Creating all tables from models (Base.metadata.create_all)..."
#docker compose exec backend env PYTHONPATH=./ python app/init_db.py

echo "🔁 Applying Alembic migrations..."
docker compose exec backend env PYTHONPATH=./ alembic -c app/alembic.ini upgrade head

echo "✅ Done! Fresh database with migrations applied."
