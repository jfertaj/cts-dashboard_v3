#!/bin/bash

echo "🔧 Running Alembic migrations..."
docker compose exec backend env PYTHONPATH=./ alembic -c app/alembic.ini upgrade head

echo "📥 Loading initial data into the database..."
docker compose exec backend python app/load_data/init_db.py

echo "✅ CTS Dashboard setup complete."
